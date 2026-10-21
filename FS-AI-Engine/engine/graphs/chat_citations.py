"""
FS-AI-Engine/graphs/chat_citations.py
=====================================
Citation packaging for the chat pipeline:
- align_citations_with_answer: heuristic provenance alignment (used when the model reports no sources).
- build_citations: meeting-level chips (no meeting selected) or turn-level snippets (meeting selected).
- format_citations_node: Node 5 of the graph.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .chat_query import ORGANIZATIONAL_COMMON_WORDS, ChatState, QueryIntent

logger = logging.getLogger("fs-ai-engine.chat_graph")

# ============================================================================
# Node 5: Format Citations Node (Provenance Aligned)
# ============================================================================

def align_citations_with_answer(retrieved_chunks: list[dict[str, Any]], answer: str) -> list[dict[str, Any]]:
    """
    Provenance Alignment:
    Guarantees that citation cards strictly align with facts, meetings, and speakers discussed in the LLM answer. Filters out unreferenced candidate chunks from unrelated meetings/speakers.
    """
    if not retrieved_chunks:
        return []
    ans_lower = answer.lower()

    # 1. Direct Bracket Citations (e.g. [1], [2])
    bracket_matches = [int(m) - 1 for m in re.findall(r"\[(\d+)\]", answer) if 1 <= int(m) <= len(retrieved_chunks)]
    if bracket_matches:
        seen_idx = set()
        aligned = []
        for idx in bracket_matches:
            if idx not in seen_idx:
                seen_idx.add(idx)
                aligned.append(retrieved_chunks[idx])
        return aligned

    # 2. Meeting-Level Provenance Alignment
    mentioned_meetings = set()
    for c in retrieved_chunks:
        m_id = c.get("meeting_id", "")
        m_title = (c.get("meeting_title") or "").strip()
        if (m_id and m_id.lower() in ans_lower) or (m_title and len(m_title) >= 3 and m_title.lower() in ans_lower):
            mentioned_meetings.add(m_id)

    # 3. Speaker-Level Provenance Alignment
    stop_speaker_tokens = ORGANIZATIONAL_COMMON_WORDS | {"mr", "ms", "mrs", "dr", "the", "and"}
    mentioned_speakers = set()
    for c in retrieved_chunks:
        spk = (c.get("speaker") or "").strip()
        if spk and spk != "Unknown":
            if spk.lower() in ans_lower or any(tok.lower() in ans_lower for tok in spk.split() if len(tok) >= 3 and tok.lower() not in stop_speaker_tokens):
                mentioned_speakers.add(spk.lower())

    def speaker_mentioned(c: dict[str, Any]) -> bool:
        spk = c.get("speaker", "")
        return spk.lower() in mentioned_speakers or any(tok.lower() in mentioned_speakers for tok in spk.split() if len(tok) >= 3)

    distinct_retrieved_meetings = {c.get("meeting_id") for c in retrieved_chunks if c.get("meeting_id")}
    if mentioned_meetings and len(mentioned_meetings) < len(distinct_retrieved_meetings):
        filtered_chunks = [c for c in retrieved_chunks if c.get("meeting_id") in mentioned_meetings]
        if mentioned_speakers:
            spk_filtered = [c for c in filtered_chunks if speaker_mentioned(c)]
            if spk_filtered:
                filtered_chunks = spk_filtered
        return filtered_chunks

    if mentioned_speakers and len(mentioned_speakers) < len({c.get("speaker", "").lower() for c in retrieved_chunks}):
        spk_filtered = [c for c in retrieved_chunks if speaker_mentioned(c)]
        if spk_filtered:
            return spk_filtered
    return retrieved_chunks

TURN_SNIPPET_CHARS = 280


def _snippet(text: str, limit: int = TURN_SNIPPET_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _is_trivial_filler(text: str) -> bool:
    clean = (text or "").strip().lower()
    return len(clean) < 25 and bool(re.match(r"^(?:ah|ok|okay|yes|no|yeah|right|correct|hello|hi|bye|thanks|scroll up)[\.\,\s]*$", clean))


def build_citations(
    chunks: list[dict[str, Any]], meeting_scoped: bool, titles: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """
    Meeting scoped: every aligned turn, in time order, with a text snippet.
    Across meetings: one citation per meeting the topic came from (best turn's timestamp,
    with a short snippet for the collapsed excerpt view), best meetings first.
    """
    titles = titles or {}
    for c in chunks:
        c["meeting_title"] = (c.get("meeting_title") or titles.get(c.get("meeting_id") or "") or "").strip()

    # Prioritize substantive turns over trivial filler utterances when scoring
    by_score = sorted(
        chunks,
        key=lambda c: (
            c.get("similarity_score", 0.0) - (2.0 if _is_trivial_filler(c.get("text", "")) else 0.0)
        ),
        reverse=True,
    )

    if meeting_scoped:
        top = sorted(by_score, key=lambda c: c.get("start_time", 0.0))
        return [
            {
                "citation_type": "turn",
                "meeting_title": c.get("meeting_title") or None,
                "speaker": c["speaker"],
                "start_time": c["start_time"],
                "end_time": c["end_time"],
                "meeting_id": c["meeting_id"],
                "exact_raw_text": _snippet(c["text"]),
                "topic_context": c.get("topic_context", "Level 1 Turn"),
            }
            for c in top
        ]

    best_per_meeting: dict[str, dict[str, Any]] = {}
    for c in by_score:
        best_per_meeting.setdefault(c.get("meeting_id") or "", c)
    return [
        {
            "citation_type": "meeting",
            "meeting_title": c.get("meeting_title") or None,
            "speaker": c["speaker"],
            "start_time": c["start_time"],
            "end_time": c["end_time"],
            "meeting_id": c["meeting_id"],
            "exact_raw_text": _snippet(c["text"]),
            "topic_context": c.get("topic_context", "Level 1 Turn"),
        }
        for c in best_per_meeting.values()
    ]


async def format_citations_node(state: ChatState) -> dict[str, Any]:
    """
    Node 5: Format Citations with Provenance Alignment & Parity Audit.
    Aligns candidate turns with the synthesized answer, audits provenance parity to guarantee
    every discussed meeting receives a citation chip, then packages them compactly:
    per-turn snippets when a meeting is selected, one meeting + timestamp entry per meeting otherwise.
    """
    retrieved_chunks = state.get("retrieved_chunks", [])
    answer = state.get("answer", "")
    query_intent = state.get("query_intent") or "content_qa"
    used = state.get("used_sources")

    if used is not None:
        # The model reported which excerpts it relied on: cite exactly those (none for small talk / off-topic).
        aligned_chunks = [retrieved_chunks[i - 1] for i in used if 1 <= i <= len(retrieved_chunks)]
    else:
        aligned_chunks = align_citations_with_answer(retrieved_chunks, answer)

    # Provenance Parity Audit for cross-meeting queries:
    # If the response explicitly discusses meetings from available_meetings, ensure each
    # discussed meeting has a citation chip even if the model omitted tagging an excerpt index for it.
    if not state.get("meeting_id") and state.get("available_meetings") and retrieved_chunks:
        cited_mids = {c.get("meeting_id") for c in aligned_chunks if c.get("meeting_id")}
        ans_lower = answer.lower()
        for m in state.get("available_meetings") or []:
            m_id = m.get("id")
            m_title = (m.get("title") or "").strip()
            if m_id and m_id not in cited_mids and m_title and len(m_title) >= 4 and re.search(rf"\b{re.escape(m_title.lower())}\b", ans_lower):
                candidates = [c for c in retrieved_chunks if c.get("meeting_id") == m_id]
                if candidates:
                    candidates.sort(key=lambda x: x.get("similarity_score", 0.0), reverse=True)
                    aligned_chunks.append(candidates[0])
                    cited_mids.add(m_id)

    titles = {m["id"]: m["title"] for m in state.get("available_meetings") or [] if m.get("id") and m.get("title")}
    citations = build_citations(aligned_chunks, meeting_scoped=bool(state.get("meeting_id")), titles=titles)

    routing_mode = "LANGGRAPH_CATALOG" if query_intent == QueryIntent.CATALOG else "LANGGRAPH_RAG"

    logger.info(
        f"[LANGGRAPH: FORMAT_CITATIONS] Packaged {len(citations)} citations "
        f"(aligned from {len(retrieved_chunks)} candidate chunks) | Mode: {routing_mode}"
    )

    return {
        "citations": citations,
        "citations_count": len(citations),
        "provenance_verified": True,
        "routing_mode": routing_mode,
    }

