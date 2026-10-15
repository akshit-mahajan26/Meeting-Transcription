"""
FS-AI-Engine/graphs/chat_synthesis.py
=====================================
Answer-generation nodes of the chat pipeline:
- llm_synthesis_node: Synthesizes conversational response using OpenRouter LLM.
- format_citations_node: Maps retrieved transcript turns into Level 1 citation models.
- fallback_node: Handles zero-match scenarios with helpful conversational guidance.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from typing import Any

from ..core.config import settings
from ..services.llm_client import get_openrouter_client
from .chat_query import (
    ChatState,
    QueryIntent,
    _state_speakers,
    normalize_entity_typos,
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
5. **Brevity:** Lead with the direct answer. Keep it to 2-5 sentences by default; use a short bullet list only \
when the user asks for several items. Add more detail only if the user explicitly asks for it (e.g. "in detail", \
"summarize everything", "explain more"). Do not restate the question or pad with background. Brevity never means dropping a meeting's date.
6. **Source Reporting:** Numbered transcript excerpts may be provided as [1], [2], ... Never write these numbers or any \
bracketed references like [3] or [11, 13] in the body of your answer. Instead, end every reply with one final line `SOURCES: ` followed by the \
comma-separated numbers of the excerpts your answer actually relied on (e.g. `SOURCES: 2, 5`). Write `SOURCES: none` \
when the answer did not come from the excerpts: greetings and small talk, questions about yourself, answers drawn only \
from the catalog or meeting-detail records, or when nothing relevant was found.
7. **Conversation History:** Earlier messages in this conversation may precede the current question. Use them only \
to understand follow-ups (e.g. "what about him?", "and the other meeting?"). Facts must still come from the excerpts \
and records supplied with the current question, not from earlier answers.
8. **Strict Grounding:** Base your response on the provided context, transcript excerpts, and database records. If \
information is missing from the context, state clearly that the records do not contain it."""


_SOURCES_LINE = re.compile(r"^[\s*_`>#-]*sources?[\s*_`]*:[\s*_`]*(.*?)[\s*_`]*$", re.IGNORECASE | re.MULTILINE)


_INLINE_MARKER = re.compile(r"[ \t]*\[\s*\d+(?:\s*[,;\u2013-]\s*\d+)*\s*\]")


def split_sources_line(answer: str, n_chunks: int) -> tuple[str, list[int] | None]:
    """
    Returns (clean answer, 1-based excerpt numbers the answer relied on).
    - Removes the trailing `SOURCES: 1, 3` line the model appends.
    - Removes inline `[11, 13]` excerpt markers the model may still write; they mean nothing to the user.
    - Numbers are limited to the excerpts provided. `none` gives []. With no usable SOURCES line, inline
      markers are used if present, else None so the caller can fall back to heuristic alignment.
    """
    def valid(nums: list[str]) -> list[int]:
        return list(dict.fromkeys(int(n) for n in nums if 1 <= int(n) <= n_chunks))

    inline = valid([n for m in _INLINE_MARKER.findall(answer) for n in re.findall(r"\d+", m)])
    matches = list(_SOURCES_LINE.finditer(answer))
    reported: list[int] | None = None
    if matches:
        last = matches[-1]
        answer = answer[: last.start()] + answer[last.end():]
        value = last.group(1).strip().lower()
        reported = [] if value in {"none", "n/a", "no sources", "-", ""} else valid(re.findall(r"\d+", value))
    clean = _INLINE_MARKER.sub("", answer).strip()
    if reported is None and inline:
        reported = inline
    return clean, reported


REWRITE_PROMPT = """Rewrite the user's message to a meeting-transcript assistant as one clean, standalone question.
- Fix typos and wrong words using the meaning of the whole sentence (e.g. "rate of the meeting" -> "date of the meeting").
- If it depends on the earlier conversation (it, they, those, "and the other one?"), make it understandable on its own.
- Keep every name, number and date the user wrote. Never answer the question or add requirements. A greeting stays a greeting.
- If it is already clear, return it unchanged.
- "date_from" and "date_to" are the inclusive range of days on which the MEETINGS were held that the question refers \
to, as YYYY-MM-DD, or null. They apply only to when a meeting took place, not to events mentioned inside a discussion \
("the outage yesterday"). Work out relative words ("today", "yesterday", "last week") from today's date. One day: both \
equal. "after X" starts the day after X, "before X" ends the day before X, "since X" starts at X, "between X and Y" is \
X to Y. If the user gave no year, write MM-DD instead (e.g. "06-12"). Both null when no meeting date or period is named.
Reply with only JSON: {"query": "<question>", "date_from": "<date or null>", "date_to": "<date or null>"}"""


async def rewrite_query(query: str, history: list[dict[str, str]]) -> tuple[str, str | None, str | None]:
    """
    One small LLM call that fixes typos, resolves follow-ups and reads the meeting date range the question refers to.
    Returns (question, date_from, date_to). Any failure returns the original query with no date range.
    """
    convo = "\n".join(f"{m['role']}: {m['content'][:400]}" for m in history[-4:])
    try:
        reply = await get_openrouter_client().generate_chat_completion(
            messages=[
                {"role": "system", "content": REWRITE_PROMPT},
                {
                    "role": "user",
                    "content": f"Today's date: {datetime.now(UTC):%Y-%m-%d}\n"
                    f"Conversation so far:\n{convo or '(none)'}\n\nUser message: {query}",
                },
            ],
            model=settings.LLM_MODEL_NAME,
            max_tokens=200,
            temperature=0.0,
        )
        parsed = json.loads(re.search(r"\{.*\}", reply, re.DOTALL).group(0))
        rewritten = str(parsed.get("query") or "").strip()
        date_from, date_to = (str(parsed[k]) if parsed.get(k) else None for k in ("date_from", "date_to"))
    except Exception as exc:  # network error, no JSON in the reply, ...
        logger.warning(f"[LANGGRAPH: REWRITE_QUERY] Keeping the original query: {exc}")
        return query, None, None
    if not rewritten or len(rewritten) > 4 * len(query) + 200:
        rewritten = query
    logger.info(f"[LANGGRAPH: REWRITE_QUERY] '{query}' -> '{rewritten}' (dates: {date_from} to {date_to})")
    return rewritten, date_from, date_to


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

    # Build prompt context blocks from retrieved transcript turns, each with its meeting's date
    m_date_map = {m["id"]: m.get("date") for m in available_meetings if m.get("id")}
    context_blocks = []
    for i, c in enumerate(retrieved_chunks):
        m_title_str = c.get("meeting_title") or c.get("meeting_id") or "Meeting"
        m_id = c.get("meeting_id") or ""
        m_date_str = c.get("meeting_date") or m_date_map.get(m_id) or c.get("date") or "not recorded"
        context_blocks.append(
            f"[{i+1}] Meeting: {m_title_str} | Held on: {m_date_str} | Topic: {c.get('topic_context')} | "
            f"Speaker: {c['speaker']} ({c['timestamp']})\n"
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
    if state.get("meeting_id"):
        detail_ids = [state["meeting_id"]]
    else:
        detail_ids = list(state.get("date_meeting_ids") or []) + [c.get("meeting_id") for c in retrieved_chunks]
    meeting_details = build_meeting_details(available_meetings, [i for i in detail_ids if i])
    if meeting_details:
        extra_record_context += meeting_details
        list_instructions.append(
            "- The Meeting Details records give each meeting's date and time (UTC). Use them when asked when a meeting "
            "took place; if a meeting shows 'not recorded', say its date is not recorded."
        )
        list_instructions.append(
            "- If the user asks about the 'last meeting' or 'latest discussion', ground your answer in the "
            "chronologically newest meeting from the verified Meeting Details records above."
        )

    # 4. Participant Involvement
    if relevant_meetings and resolved_speakers:
        spk_label = ", ".join(resolved_speakers)
        mtg_bullets = "\n".join(
            f"- **{m['title']}** (Held on: {m_date_map.get(m['id']) or 'not recorded'}, {m.get('turns', 0)} turns)"
            for m in relevant_meetings
        )
        extra_record_context += (
            f"Database Records - Verified Meetings with Participant(s) {spk_label} ({len(relevant_meetings)} total):\n"
            f"{mtg_bullets}\n\n---\n"
        )
        list_instructions.append(
            f"- The user is asking about meetings involving {spk_label}. List all {len(relevant_meetings)} verified meetings from the Database Records above, one short line each (title and date)."
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
        f"- Provide a direct, professional, conversational answer. Be concise: 2-5 sentences unless the user asks for detail.\n"
        f"- Never dump raw transcript turns, timestamps, or quotation lists into the main text.\n"
        f"{instruction_str}\n"
        f"- Ground your statements strictly in the facts provided in the excerpts and records."
    )

    llm_client = get_openrouter_client()
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
