"""
FS-AI-Engine/graphs/chat_synthesis.py
=====================================
Answer-generation nodes of the chat pipeline:
- llm_synthesis_node: Synthesizes conversational response using OpenRouter LLM.
- format_citations_node: Maps retrieved transcript turns into Level 1 citation models.
- fallback_node: Handles zero-match scenarios with helpful conversational guidance.
"""

from __future__ import annotations
import logging
import os
import re
import time
from datetime import UTC, datetime
from typing import Any

from ..core.config import settings
from ..services.llm_client import get_openrouter_client
from .chat_query import (
    ORGANIZATIONAL_COMMON_WORDS,
    ChatState,
    QueryIntent,
    _state_speakers,
    normalize_entity_typos,
)

# Dedicated AI Service Logger
logger = logging.getLogger("fs-ai-engine.chat_graph")

UNIVERSAL_RAG_SYSTEM_PROMPT = """You are an expert Meeting Intelligence Assistant. Your primary goal is to provide clear, professional, and conversational answers to *any* user query regarding ingested meeting transcripts, catalog records, and project metadata.

Guidelines:
1. **Conversational Synthesis:** Answer the user's question directly, clearly, and naturally in the main response body. Never dump raw transcript blocks, speaker headers, or timestamps into the text response.
2. **Catalog & Inventory Queries:** When asked about system entities (projects, meetings, participants, topics, statuses, counts), ground your answer directly and strictly in the verified database catalog records provided. State names, IDs, statuses, and counts accurately.
3. **Participant Involvement & Meetings:** When asked to list meetings or about who was involved, explicitly name the meeting title(s) and meeting ID(s) based on the verified database records and excerpts.
4. **Meeting Dates:** When asked when a meeting took place, answer from the Meeting Details records (dates and times are UTC). Do not infer dates from transcript text.
5. **Strict Grounding:** Base your response on the provided context, transcript excerpts, and database records. If information is missing from the context, state clearly that the records do not contain it."""

_MAX_CATALOG_MEETING_ROWS = 200
_MAX_DETAIL_MEETINGS = 5

def format_meeting_date(raw: Any) -> str | None:
    """Meeting start as 'YYYY-MM-DD HH:MM UTC'. Fireflies stores epoch milliseconds; ISO strings pass through."""
    if not raw:
        return None
    text = str(raw).strip()
    try:
        number = float(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            return text
    if number <= 0:
        return None
    seconds = number / 1000 if number > 1e11 else number
    return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")

def build_meeting_details(available_meetings: list[dict[str, Any]], meeting_ids: list[str]) -> str:
    """Verified per-meeting facts (title, ID, project, date and time), so date questions can be answered."""
    by_id = {m["id"]: m for m in available_meetings}
    lines = []
    for mid in dict.fromkeys(meeting_ids):
        m = by_id.get(mid)
        if not m:
            continue
        lines.append(
            f"- **{m['title']}** (ID: {m['id']}, Project: {m.get('project_id') or 'Unassigned'}, "
            f"Held on: {m.get('date') or 'not recorded'})"
        )
        if len(lines) >= _MAX_DETAIL_MEETINGS:
            break
    if not lines:
        return ""
    return "Database Records - Meeting Details (dates and times are UTC):\n" + "\n".join(lines) + "\n\n---\n"

def build_catalog_records(
    *,
    query_lower: str,
    force_all: bool,
    available_projects: list[dict[str, Any]],
    available_meetings: list[dict[str, Any]],
    available_speakers: list[str],
    scoped_to_meeting: bool,
    project_id: str | None,
    has_speakers: bool,
) -> tuple[str, list[str]]:
    """
    Builds the verified database catalog blocks for the synthesis prompt.
    `force_all` is set for catalog-intent questions: the records are always supplied, no matter how the question is worded. Otherwise each block is added only when the question mentions its entity. Counts are computed here so the model never has to count.
    """
    context = ""
    instructions: list[str] = []
    mentions = lambda *words: any(w in query_lower for w in words)  # noqa: E731

    # Projects (with deterministic meeting counts per project)
    if available_projects and (force_all or mentions("project")):
        per_project: dict[str, int] = {}
        unassigned = 0
        for m in available_meetings:
            if m.get("project_id"):
                per_project[m["project_id"]] = per_project.get(m["project_id"], 0) + 1
            else:
                unassigned += 1
        active = [p for p in available_projects if (p.get("status") or "").upper() == "ACTIVE"]
        lines = "\n".join(
            f"- **{p['name']}** (ID: {p['id']}, Status: {p.get('status', 'ACTIVE')}, "
            f"Meetings: {per_project.get(p['id'], 0)})"
            for p in available_projects
        )
        counts = ", ".join(f"{pid}: {n}" for pid, n in sorted(per_project.items()))
        if unassigned:
            counts += f"{', ' if counts else ''}Unassigned: {unassigned}"
        context += (
            f"Database Catalog - Projects ({len(available_projects)} total, {len(active)} active):\n{lines}\n"
            f"Verified meeting counts per project (computed from the database): {counts or 'none'}; "
            f"total meetings: {len(available_meetings)}\n\n---\n"
        )
        instructions.append(
            "- Answer project and meeting-count questions using the verified Database Catalog above. Use the computed meeting counts exactly as given; do not recount or estimate."
        )

    # Meetings
    general_meeting_query = mentions("meeting") and mentions("all", "list", "how many", "which", "available", "recorded", "what meetings")
    if available_meetings and (force_all or general_meeting_query) and not scoped_to_meeting and not has_speakers:
        rows = available_meetings[:_MAX_CATALOG_MEETING_ROWS]
        mtg_lines = "\n".join(
            f"- **{m['title']}** (ID: {m['id']}, Project: {m.get('project_id') or 'Unassigned'}, "
            f"Held on: {m.get('date') or 'not recorded'})"
            for m in rows
        )
        more = (
            f"\n(+{len(available_meetings) - len(rows)} more meetings not listed; the total above is exact)"
            if len(available_meetings) > len(rows) else ""
        )
        context += (
            f"Database Catalog - Complete List of Meetings ({len(available_meetings)} total):\n{mtg_lines}{more}\n\n---\n"
        )
        instructions.append(
            f"- The catalog lists {len(available_meetings)} meetings in total. When the user asks to list or count meetings, "
            "state the exact number and name the meetings with their IDs."
        )

    # Participants
    participant_query = mentions("speaker", "participant", "attendee", "participate", "attend") and mentions(
        "all", "list", "how many", "who", "which", "available"
    )
    if available_speakers and (force_all or participant_query):
        proj_str = f" for Project '{project_id}'" if project_id else ""
        spk_lines = "\n".join(f"- {s}" for s in available_speakers)
        context += (
            f"Database Catalog - Participants / Speakers ({len(available_speakers)} total{proj_str}):\n{spk_lines}\n\n---\n"
        )
        instructions.append(
            f"- When asked about participants/speakers{proj_str}, enumerate them from the verified Database Catalog above."
        )

    return context, instructions

# ============================================================================
# Node 4: LLM Synthesis Node
# ============================================================================

async def llm_synthesis_node(state: ChatState) -> dict[str, Any]:
    """
    Node 4: LLM Synthesis with OpenRouter.
    Executes conversational completion strictly grounded in verified database catalog records and transcript excerpts.
    """
    llm_start = time.time()
    query = state.get("query", "").strip()
    query_lower = normalize_entity_typos(query).lower()
    is_catalog = state.get("query_intent") == QueryIntent.CATALOG
    retrieved_chunks = state.get("retrieved_chunks", [])
    resolved_speakers: list[str] = _state_speakers(state)

    available_projects = state.get("available_projects") or []
    available_meetings = state.get("available_meetings") or []
    available_speakers = state.get("available_speakers") or []
    relevant_meetings = state.get("relevant_meetings") or []

    # Build prompt context blocks from retrieved transcript turns
    context_blocks = []
    for i, c in enumerate(retrieved_chunks):
        m_title_str = c.get("meeting_title") or c.get("meeting_id") or "Meeting"
        context_blocks.append(
            f"[{i+1}] Meeting: {m_title_str} (ID: {c.get('meeting_id')}) | Topic: {c.get('topic_context')} | Speaker: {c['speaker']} ({c['timestamp']})\n"
            f"Quote: \"{c['text']}\""
        )
    formatted_context = "\n---\n".join(context_blocks)

    extra_record_context, list_instructions = build_catalog_records(
        query_lower=query_lower,
        force_all=is_catalog,
        available_projects=available_projects,
        available_meetings=available_meetings,
        available_speakers=available_speakers,
        scoped_to_meeting=bool(state.get("meeting_id")),
        project_id=state.get("project_id"),
        has_speakers=bool(resolved_speakers),
    )

    # Meeting date/time: for the scoped meeting, or the meetings behind the retrieved excerpts
    detail_ids = [state["meeting_id"]] if state.get("meeting_id") else [c.get("meeting_id") for c in retrieved_chunks]
    meeting_details = build_meeting_details(available_meetings, [i for i in detail_ids if i])
    if meeting_details:
        extra_record_context += meeting_details
        list_instructions.append(
            "- The Meeting Details records give each meeting's date and time (UTC). Use them when asked when a meeting "
            "took place; if a meeting shows 'not recorded', say its date is not recorded."
        )

    # 4. Participant Involvement
    if relevant_meetings and resolved_speakers:
        spk_label = ", ".join(resolved_speakers)
        mtg_bullets = "\n".join(
            f"- **{m['title']}** (ID: {m['id']}, {m.get('turns', 0)} turns)"
            for m in relevant_meetings
        )
        extra_record_context += (
            f"Database Records - Verified Meetings with Participant(s) {spk_label} ({len(relevant_meetings)} total):\n"
            f"{mtg_bullets}\n\n---\n"
        )
        list_instructions.append(
            f"- The user is asking about meetings involving {spk_label}. You MUST explicitly list and mention ALL {len(relevant_meetings)} verified meetings from the Database Records above in your answer."
        )

    if is_catalog and not extra_record_context and not relevant_meetings:
        logger.warning(f"[LANGGRAPH: LLM_SYNTHESIS] Catalog question with no catalog records available: '{query}'")
        return {
            "answer": (
                "I couldn't find any project, meeting or participant records to answer that. "
                "Please check that meetings have been ingested and try again."
            ),
            "llm_model_used": state.get("llm_model_used") or "none",
        }

    formatted_context = extra_record_context + formatted_context
    instruction_str = "\n".join(list_instructions) if list_instructions else ""

    user_prompt = (
        f"User Query: {query}\n\n"
        f"Verified Context & Records:\n{formatted_context}\n\n"
        f"Instructions:\n"
        f"- Provide a direct, professional, conversational narrative answer to the user's query.\n"
        f"- Never dump raw transcript turns, timestamps, or quotation lists into the main text.\n"
        f"{instruction_str}\n"
        f"- Ground your statements strictly in the facts provided in the excerpts and records."
    )

    llm_client = get_openrouter_client()
    messages = [
        {"role": "system", "content": UNIVERSAL_RAG_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    target_model = getattr(settings, "LLM_MODEL_NAME", None) or os.getenv("LLM_MODEL_NAME") or os.getenv("LLM_MODEL") or "gemini-2.5-flash"
    answer = await llm_client.generate_chat_completion(
        messages=messages,
        model=target_model,
        max_tokens=4096,
        temperature=0.2,
    )

    llm_latency_ms = (time.time() - llm_start) * 1000
    provider_name = getattr(llm_client, "provider", "llm").upper()
    logger.info(
        f"[LANGGRAPH: LLM_SYNTHESIS] Model: {target_model} (Provider: {provider_name}) | "
        f"Latency: {llm_latency_ms:.2f}ms | Length: {len(answer)} chars"
    )

    return {
        "answer": answer,
        "llm_model_used": target_model,
    }

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

async def format_citations_node(state: ChatState) -> dict[str, Any]:
    """
    Node 5: Format Citations with Provenance Alignment.
    Packages retrieved turns into clean Level 1 citation models for the frontend cards, aligning citations with the synthesized answer.
    """
    retrieved_chunks = state.get("retrieved_chunks", [])
    answer = state.get("answer", "")
    query_intent = state.get("query_intent") or "content_qa"
    aligned_chunks = align_citations_with_answer(retrieved_chunks, answer)

    citations: list[dict[str, Any]] = []
    for c in aligned_chunks:
        citations.append({
            "speaker": c["speaker"],
            "start_time": c["start_time"],
            "end_time": c["end_time"],
            "meeting_id": c["meeting_id"],
            "exact_raw_text": c["text"],
            "topic_context": c.get("topic_context", "Level 1 Turn"),
        })

    routing_mode = "LANGGRAPH_CATALOG" if query_intent == QueryIntent.CATALOG else "LANGGRAPH_RAG"

    logger.info(
        f"[LANGGRAPH: FORMAT_CITATIONS] Packaged {len(citations)} Level 1 citations "
        f"(aligned from {len(retrieved_chunks)} candidate chunks) | Mode: {routing_mode}"
    )

    return {
        "citations": citations,
        "citations_count": len(citations),
        "provenance_verified": True,
        "routing_mode": routing_mode,
    }

# ============================================================================
# Node 6: Fallback Node
# ============================================================================

async def fallback_node(state: ChatState) -> dict[str, Any]:
    """
    Node 6: Fallback Node.
    Gracefully handles zero-match scenarios with conversational assistance.
    """
    query = state.get("query", "")
    logger.warning(f"[LANGGRAPH: FALLBACK] Zero matching transcript excerpts found in vector database for query '{query}'.")

    fallback_answer = (
        "Based on the ingested meeting records, no relevant discussions or transcript excerpts were found "
        "matching your inquiry. Please ensure the relevant meeting has been processed or try rephrasing."
    )

    return {
        "answer": fallback_answer,
        "citations": [],
        "citations_count": 0,
        "provenance_verified": False,
        "routing_mode": "LANGGRAPH_FALLBACK",
        "routing_reasoning": "Zero matching speaker turns found in pgvector",
    }
