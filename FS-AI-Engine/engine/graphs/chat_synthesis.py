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
from typing import Any, Optional

from langgraph.config import get_stream_writer
from langgraph.types import RunnableConfig

from ..core.config import settings
from ..services.llm_client import get_llm_client
from .chat_query import (
    ChatState,
    QueryIntent,
    _state_speakers,
    is_name_token_match,
)
from .chat_records import build_catalog_records, build_meeting_details

# Dedicated AI Service Logger
logger = logging.getLogger("fs-ai-engine.chat_graph")

def get_system_prompt(now_utc: datetime | None = None) -> str:
    """Constructs the universal RAG system prompt with live temporal metadata reference."""
    now = now_utc or datetime.now(UTC)
    current_moment_str = now.strftime("%A, %B %d, %Y at %H:%M UTC")
    iso_date = now.strftime("%Y-%m-%d")
    day_name = now.strftime("%A")

    return f"""You are an expert Meeting Intelligence Assistant. Your primary goal is to provide clear, \
professional, and conversational answers to *any* user query regarding ingested meeting transcripts, catalog \
records, and project metadata.

System Temporal Reference (Query Execution Time):
- **Current Moment:** {current_moment_str}
- **Today's Date:** {iso_date} ({day_name})
- **Note:** This timestamp represents the exact present moment when the user is asking their question. It is \
distinct from the historical occurrence dates and within-meeting audio timestamps of the ingested transcripts.

Guidelines:
1. **Conversational Synthesis:** Answer the user's question directly, clearly, and naturally in the main response \
body. Never dump raw transcript blocks, speaker headers, or timestamps into the text response.
2. **Catalog & Inventory Queries:** When asked about system entities (projects, meetings, participants, topics, \
statuses, counts), ground your answer directly and strictly in the verified database catalog records provided. \
State names, statuses, and counts accurately. Never show internal meeting IDs in your answer; whenever you name a \
meeting, give its title and the date it was held on (from the 'Held on' records), e.g. 'Weekly Sync (14 Oct 2025)'.
3. **Participant Involvement & Meetings:** When asked to list meetings or about who was involved, explicitly name \
the meeting title(s) and the date each was held on (never the meeting ID) based on the verified database records and excerpts.
4. **Meeting Dates & Temporal Accuracy:**
   - When asked when a meeting took place, answer strictly from the verified Meeting Details records (dates and \
times are UTC).
   - Use the **Current Moment** ({current_moment_str}) as the absolute reference anchor for all relative time \
queries ('today', 'yesterday', 'this week', 'last week', etc.).
   - Distinguish sharply between the current execution date and the historical dates when past meetings occurred.
   - NEVER confuse dates, weekdays, or relative time phrases spoken *inside* past meeting transcripts (e.g., when a \
speaker in a past recording said "yesterday we had an outage" or "let's sync this Friday") with the user's present \
day. Those historical statements apply only to the day of that past recording.
   - If a user asks about events on a specific date or timeframe (e.g., "what happened on October 14th" or "what \
did we discuss yesterday"), check the verified "Held on" date of the meetings. If no meeting was held on that date, \
state clearly that no meetings were recorded for that date.
5. **Response Depth & Brevity:** Always be direct, well-structured, and concise. Avoid rambling essays, repetitive \
background padding, or multi-section dissertations.
   - For direct factual lookups: Keep it concise: 2-4 sentences.
   - For broader synthesis questions, comparisons, or follow-up explanations (e.g. "how do X and Y relate?", "can you explain it?"): \
Provide a clear, focused synthesis in 1 to 2 short paragraphs or a compact bulleted list of 3-4 points (aim for roughly 100-180 words).
   - Never pad with unnecessary headings or transcript narration. Brevity never means dropping a meeting's date.
6. **Source Reporting:** Numbered transcript excerpts may be provided as [1], [2], ... Never write these numbers or any \
bracketed references like [3] or [11, 13] in the body of your answer. Instead, end every reply with one final line `SOURCES: ` followed by the \
comma-separated numbers of the excerpts your answer actually relied on (e.g. `SOURCES: 2, 5`). Write `SOURCES: none` \
when the answer did not come from the excerpts: greetings and small talk, questions about yourself, answers drawn only \
from the catalog or meeting-detail records, or when nothing relevant was found.
7. **Conversation History & Progression:** Earlier messages in this conversation may precede the current question.
   - Use history to understand conversational references and follow-ups (e.g. "what about him?", "can you explain it?").
   - Conversational Progression: When the user asks a follow-up (such as asking to explain, elaborate, or follow up on a \
previous answer), do NOT repeat the previous assistant answer or give the same high-level summary. Treat the previous \
answer as already established context, and move forward by digging deeper into the transcript excerpts to unpack the \
specific details, explanations, dialogue, and reasons discussed by the participants.
   - Facts must still come from the excerpts and records supplied with the current question, not from hallucinated claims.
8. **Strict Grounding:** Base your response on the provided context, transcript excerpts, and database records. If \
information is missing from the context, state clearly that the records do not contain it."""


_SOURCES_LINE = re.compile(r"^[\s*_`>#-]*sources?[\s*_`]*:[\s*_`]*(.*?)[\s*_`]*$", re.IGNORECASE | re.MULTILINE)


_INLINE_MARKER = re.compile(r"[ \t]*\[\s*\d+(?:\s*[,;\u2013-]\s*\d+)*\s*\]")

_SOURCES_START = re.compile(r"^sources?[\s*_`]*(:.*)?$", re.IGNORECASE)
_OPEN_MARKER = re.compile(r"[ \t]*(?:\[[\s\d,;\u2013-]*)?$")  # trailing spaces and/or an unfinished `[1, 2`: may belong to a marker
_LINE_LEAD = " \t*_`>#-"


def _could_be_sources_line(line: str) -> bool:
    """True while `line` is still compatible with a `SOURCES: ...` line (so it must not be shown yet)."""
    core = line.lstrip(_LINE_LEAD)
    return not core or "sources".startswith(core.lower()) or bool(_SOURCES_START.match(core))


class AnswerStreamFilter:
    """
    Cleans a streamed answer the way `split_sources_line` cleans the final one, so the reader never sees the
    `SOURCES:` line or `[3]` excerpt markers flash by: lines that might still turn out to be a SOURCES line are
    held back, and so are trailing spaces and an unfinished `[1, 2` marker. The final event carries the canonical text regardless.
    """

    def __init__(self) -> None:
        self._pending = ""  # text of the current line that has not been released yet
        self._live = False  # the current line cannot be a SOURCES line any more, so it is released as it arrives

    def feed(self, delta: str) -> str:
        """Takes the next text delta and returns the text that is safe to show now."""
        out: list[str] = []
        for ch in delta:
            if ch == "\n":
                out.append(self._finish_line())
                continue
            self._pending += ch
            if not self._live and not _could_be_sources_line(self._pending):
                self._live = True
        if self._live:
            cut = _OPEN_MARKER.search(self._pending)
            ready, self._pending = (self._pending[: cut.start()], self._pending[cut.start():]) if cut else (self._pending, "")
            out.append(_INLINE_MARKER.sub("", ready))
        return "".join(out)

    def flush(self) -> str:
        """Called when the stream ends: releases whatever is still held."""
        line, live = self._pending, self._live
        self._pending, self._live = "", False
        if not live and _SOURCES_LINE.match(line):
            return ""
        return _INLINE_MARKER.sub("", line)

    def _finish_line(self) -> str:
        line, live = self._pending, self._live
        self._pending, self._live = "", False
        if not live and _SOURCES_LINE.match(line):
            return ""  # the SOURCES line and its newline are dropped
        return _INLINE_MARKER.sub("", line) + "\n"


def split_sources_line(answer: str, n_chunks: int) -> tuple[str, list[int] | None]:
    """
    Returns (clean answer, 1-based excerpt numbers the answer relied on).
    - Removes all `SOURCES: ...` lines the model appends (both per-section and trailing).
    - Removes inline `[11, 13]` excerpt markers the model may still write; they mean nothing to the user.
    - Numbers are limited to the excerpts provided. `none` gives [].
    - Aggregates reported source indices across all occurrences in the answer text.
    - With no usable SOURCES line, inline markers are used if present, else None so the caller
      can fall back to heuristic alignment.
    """
    def valid(nums: list[str]) -> list[int]:
        return list(dict.fromkeys(int(n) for n in nums if 1 <= int(n) <= n_chunks))

    inline = valid([n for m in _INLINE_MARKER.findall(answer) for n in re.findall(r"\d+", m)])
    matches = list(_SOURCES_LINE.finditer(answer))
    reported: list[int] | None = None
    if matches:
        all_nums: list[str] = []
        has_numbers = False
        all_none = True
        for m in matches:
            value = m.group(1).strip().lower()
            if value in {"none", "n/a", "no sources", "-", ""}:
                continue
            all_none = False
            nums = re.findall(r"\d+", value)
            if nums:
                has_numbers = True
                all_nums.extend(nums)

        if has_numbers:
            reported = valid(all_nums)
        elif all_none:
            reported = []
        else:
            reported = None

        # Globally remove all SOURCES lines so none leak into user-facing output
        answer = _SOURCES_LINE.sub("", answer)

    clean = _INLINE_MARKER.sub("", answer).strip()
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    if reported is None and inline:
        reported = inline
    return clean, reported



async def _stream_completion(llm_client: Any, messages: list[dict[str, str]], model: str) -> str:
    """
    Streams the completion to the graph's custom stream as {"type": "token", "text": ...} and returns the full text.
    If the stream breaks, the answer is generated again in one piece; the final result then replaces what was shown.
    """
    write = get_stream_writer()
    clean = AnswerStreamFilter()
    parts: list[str] = []
    try:
        async for delta in llm_client.stream_chat_completion(messages=messages, model=model, max_tokens=4096, temperature=0.2):
            parts.append(delta)
            if text := clean.feed(delta):
                write({"type": "token", "text": text})
        if text := clean.flush():
            write({"type": "token", "text": text})
    except Exception as exc:
        logger.warning(f"[LANGGRAPH: LLM_SYNTHESIS] Streaming failed, generating the answer in one piece: {exc}")
        return await llm_client.generate_chat_completion(messages=messages, model=model, max_tokens=4096, temperature=0.2)
    return "".join(parts).strip()


# ============================================================================
# Node 4: LLM Synthesis Node
# ============================================================================

# LangGraph only injects `config` for these exact annotations (a `RunnableConfig | None` string is silently skipped)
async def llm_synthesis_node(state: ChatState, config: Optional[RunnableConfig] = None) -> dict[str, Any]:  # noqa: UP045
    """
    Node 4: LLM Synthesis with OpenRouter.
    Executes conversational completion strictly grounded in verified database catalog records and transcript excerpts.
    When the run was started with `configurable.stream_answer`, the answer is also emitted token by token on the
    graph's custom stream; the returned state update is the same either way.
    """
    llm_start = time.time()
    query = state.get("query", "").strip()
    is_catalog = state.get("query_intent") == QueryIntent.CATALOG
    retrieved_chunks = state.get("retrieved_chunks", [])
    resolved_speakers: list[str] = _state_speakers(state)

    available_projects = state.get("available_projects") or []
    available_meetings = state.get("available_meetings") or []
    available_speakers = state.get("available_speakers") or []
    relevant_meetings = state.get("relevant_meetings") or []

    # Build prompt context blocks from retrieved transcript turns, each with its meeting's date
    m_date_map = {m["id"]: m.get("date") for m in available_meetings if m.get("id")}
    context_blocks = []
    for i, c in enumerate(retrieved_chunks):
        m_title_str = c.get("meeting_title") or c.get("meeting_id") or "Meeting"
        m_id = c.get("meeting_id") or ""
        m_date_str = c.get("meeting_date") or m_date_map.get(m_id) or c.get("date") or "not recorded"

        spk_tag = f"Speaker: {c['speaker']} ({c['timestamp']})"
        if resolved_speakers:
            c_spk_lower = (c.get("speaker") or "").lower()
            is_speaker = any(
                s.lower() == c_spk_lower
                or is_name_token_match(s.split()[0], c_spk_lower)
                or s.lower() in c_spk_lower
                for s in resolved_speakers
            )
            if is_speaker:
                spk_tag += f" [PARTICIPANT SPOKE: {c['speaker']} spoke this turn]"
            else:
                spk_tag += f" [PARTICIPANT MENTION ONLY: Spoken by {c['speaker']}, NOT by {', '.join(resolved_speakers)}]"

        context_blocks.append(
            f"[{i+1}] Meeting: {m_title_str} | Held on: {m_date_str} | Topic: {c.get('topic_context')} | {spk_tag}\n"
            f"Quote: \"{c['text']}\""
        )
    formatted_context = "\n---\n".join(context_blocks)

    extra_record_context, list_instructions = build_catalog_records(
        entities=set(state.get("catalog_entities") or []),
        force_all=is_catalog,
        available_projects=available_projects,
        available_meetings=available_meetings,
        available_speakers=available_speakers,
        scoped_to_meeting=bool(state.get("meeting_id")) and not is_catalog and not state.get("meeting_scope_inferred"),
        project_id=state.get("project_id"),
        has_speakers=bool(resolved_speakers),
    )

    # Meeting date/time: for catalog queries, ordinal queries, or inferred scoping, include all available meetings
    # so the synthesis model has the complete chronological timeline; for explicit single-meeting content Q&A, scope to that meeting.
    if is_catalog or state.get("meeting_scope_inferred") or not state.get("meeting_id"):
        detail_ids = [m["id"] for m in available_meetings]
    else:
        detail_ids = [state["meeting_id"]]
    meeting_details = build_meeting_details(available_meetings, [i for i in detail_ids if i])
    if meeting_details:
        extra_record_context += meeting_details
        list_instructions.append(
            "- The Meeting Details records give each meeting's date and time (UTC). Use them when asked when a meeting "
            "took place; if a meeting shows 'not recorded', say its date is not recorded."
        )
        list_instructions.append(
            "- If the user asks about a relative or ordinal meeting (e.g. 'last meeting', 'first meeting', 'second discussion'), "
            "ground your answer in the chronologically ordered verified Meeting Details records above."
        )

    # 4. Participant Involvement
    if relevant_meetings and resolved_speakers:
        spk_label = ", ".join(resolved_speakers)
        time_scope = state.get("time_scope")
        if time_scope in ("latest", "earliest") and len(relevant_meetings) == 1:
            m = relevant_meetings[0]
            m_date = m.get("date") or m_date_map.get(m["id"]) or "not recorded"
            temporal_desc = "Most Recent" if time_scope == "latest" else "Earliest"
            extra_record_context += (
                f"Database Records - {temporal_desc} Verified Meeting with Participant {spk_label}:\n"
                f"- **{m['title']}** (Held on: {m_date}, {m.get('turns', 0)} turns spoken)\n\n---\n"
            )
            list_instructions.append(
                f"- The user is asking for the {temporal_desc.lower()} meeting {spk_label} attended / was part of. "
                f"State directly and clearly that the {temporal_desc.lower()} meeting {spk_label} attended was **{m['title']}** on {m_date}. "
                f"Do NOT mention any subsequent meeting where {spk_label} was absent or only mentioned."
            )
        else:
            mtg_bullets = "\n".join(
                f"- **{m['title']}** (Held on: {m.get('date') or m_date_map.get(m['id']) or 'not recorded'}, {m.get('turns', 0)} turns spoken)"
                for m in relevant_meetings
            )
            extra_record_context += (
                f"Database Records - Verified Meetings with Participant(s) {spk_label} ({len(relevant_meetings)} total):\n"
                f"{mtg_bullets}\n\n---\n"
            )
            list_instructions.append(
                f"- The user is asking about meetings involving {spk_label}. List all {len(relevant_meetings)} verified meetings from the Database Records above, one short line each (title and date)."
            )
    elif resolved_speakers and state.get("query_intent") == QueryIntent.PARTICIPANT_INVOLVEMENT:
        spk_label = ", ".join(resolved_speakers)
        extra_record_context += (
            f"Database Records - Verified Meetings with Participant(s) {spk_label} (0 total):\n"
            f"- No recorded meetings found where {spk_label} attended or spoke.\n\n---\n"
        )
        list_instructions.append(
            f"- State directly and clearly that {spk_label} has not attended or spoken in any recorded meetings."
        )

    if resolved_speakers and retrieved_chunks:
        list_instructions.append(
            f"- Attendance & Speaker Attribution: Distinguish strictly between when {', '.join(resolved_speakers)} SPOKE vs when they were ONLY MENTIONED by others. "
            f"Quotes marked '[PARTICIPANT MENTION ONLY]' show other attendees speaking (e.g. discussing their absence or work); "
            f"never treat a mention or note of absence as evidence that {', '.join(resolved_speakers)} joined or was part of that meeting."
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

    has_history = bool(state.get("history"))
    is_explanatory = state.get("response_depth") == "explanatory"
    if is_explanatory:
        progression_note = (
            " Do NOT repeat previous answers verbatim; move forward by synthesizing the relevant details."
            if has_history
            else ""
        )
        list_instructions.append(
            f"- FOCUSED SYNTHESIS:{progression_note} Explain the core discussion, rationale, and participant context directly from the excerpts. "
            "Keep the response focused in 1 to 2 concise paragraphs (or 3-4 compact bullet points); do not write an overly long essay or pad with repetitive narrative."
        )

    formatted_context = extra_record_context + formatted_context
    instruction_str = "\n".join(list_instructions) if list_instructions else ""

    depth_instruction = (
        "- Synthesize the explanation clearly and directly in 1 to 2 concise paragraphs (or a compact bulleted list of 3-4 points). Highlight the key decisions, technical rationale, and participant context without writing an overly long essay."
        if is_explanatory
        else "- Provide a direct, professional, conversational answer. Keep it concise: 2-4 sentences (or a short bullet list if naming multiple items)."
    )

    user_prompt = (
        f"User Query: {query}\n\n"
        f"Verified Context & Records:\n{formatted_context}\n\n"
        f"Instructions:\n"
        f"{depth_instruction}\n"
        f"- Never dump raw transcript turns, timestamps, or quotation lists into the main text.\n"
        f"{instruction_str}\n"
        f"- Ground your statements strictly in the facts provided in the excerpts and records."
    )

    llm_client = get_llm_client()
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in state.get("history") or []
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    messages = [
        {"role": "system", "content": get_system_prompt()},
        *history,
        {"role": "user", "content": user_prompt},
    ]

    target_model = getattr(settings, "LLM_MODEL_NAME", None) or os.getenv("LLM_MODEL_NAME") or os.getenv("LLM_MODEL") or "gemini-2.5-flash"
    if (config or {}).get("configurable", {}).get("stream_answer") and hasattr(llm_client, "stream_chat_completion"):
        answer = await _stream_completion(llm_client, messages, target_model)
    else:
        answer = await llm_client.generate_chat_completion(
            messages=messages,
            model=target_model,
            max_tokens=4096,
            temperature=0.2,
        )

    answer, used_sources = split_sources_line(answer, len(retrieved_chunks))

    llm_latency_ms = (time.time() - llm_start) * 1000
    provider_name = getattr(llm_client, "provider", "llm").upper()
    logger.info(
        f"[LANGGRAPH: LLM_SYNTHESIS] Model: {target_model} (Provider: {provider_name}) | "
        f"Latency: {llm_latency_ms:.2f}ms | Length: {len(answer)} chars"
    )

    return {
        "answer": answer,
        "used_sources": used_sources,
        "llm_model_used": target_model,
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

    if state.get("no_meeting_on_date"):
        fallback_answer = (
            f"No meetings are recorded for {state['no_meeting_on_date']}. "
            "Ask about a different date, or ask for a list of the recorded meetings."
        )
    else:
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
