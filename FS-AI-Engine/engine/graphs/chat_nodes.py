"""
LangGraph Nodes for the Dedicated AI Engine Chatbot Pipeline.

Implements the modular node functions executed within the ChatState StateGraph:
1. extract_filters_node: Dynamically resolves participant names, meeting boundaries, and query intent (catalog vs participant involvement vs content Q&A) from DB records.
2. embed_query_node: Generates 1536-dimensional query vector embeddings.
3. vector_retrieval_node: Executes PostgreSQL pgvector cosine similarity search with metadata filtering.
4. llm_synthesis_node: Synthesizes conversational response using OpenRouter LLM.
5. format_citations_node: Maps retrieved transcript turns into Level 1 citation models.
6. fallback_node: Handles zero-match scenarios with helpful conversational guidance.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import text

from ..core.database import AsyncSessionLocal
from .chat_citations import align_citations_with_answer, format_citations_node
from .chat_query import (
    MEETING_STOP_WORDS,
    ORGANIZATIONAL_COMMON_WORDS,
    ChatState,
    QueryIntent,
    _speaker_clause,
    _state_speakers,
    classify_query_intent,
    is_name_token_match,
    is_pure_inventory_question,
    normalize_entity_typos,
    resolve_meeting_from_query,
    resolve_speakers_from_query,
)
from .chat_records import build_catalog_records, format_meeting_date
from .chat_retrieval import embed_query_node, enrich_chunk_text, vector_retrieval_node
from .chat_synthesis import fallback_node, llm_synthesis_node, rewrite_query

# Dedicated AI Service Logger
logger = logging.getLogger("fs-ai-engine.chat_graph")

# Public surface of the former single-module chat_nodes (kept importable from here)
__all__ = [
    "MEETING_STOP_WORDS",
    "ORGANIZATIONAL_COMMON_WORDS",
    "ChatState",
    "QueryIntent",
    "align_citations_with_answer",
    "build_catalog_records",
    "classify_query_intent",
    "embed_query_node",
    "enrich_chunk_text",
    "extract_filters_node",
    "fallback_node",
    "format_citations_node",
    "is_name_token_match",
    "is_pure_inventory_question",
    "llm_synthesis_node",
    "normalize_entity_typos",
    "resolve_meeting_from_query",
    "resolve_speakers_from_query",
    "vector_retrieval_node",
]


def _unscope_proj(pid: str | None) -> str | None:
    if pid and ":" in pid and len(pid.split(":", 1)[0]) == 36:
        return pid.split(":", 1)[1]
    return pid


# Date resolution: which meetings does the question's date range refer to? (the range comes from rewrite_query)

def _meeting_day(meeting: dict[str, Any]) -> date | None:
    """Calendar day of a meeting from its stored 'YYYY-MM-DD HH:MM UTC' date, or None if unreadable."""
    try:
        return date.fromisoformat(str(meeting.get("date") or "")[:10])
    except ValueError:
        return None


def _resolve_day(value: str | None, held: list[date]) -> date | None:
    """'YYYY-MM-DD' as given; 'MM-DD' (no year stated) takes the newest year with a meeting on that day."""
    value = (value or "").strip()
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    try:
        month, day = (int(part) for part in value.split("-"))
        return date(max((d.year for d in held if (d.month, d.day) == (month, day)), default=datetime.now(UTC).year), month, day)
    except ValueError:
        return None


def meetings_in_date_range(
    meetings: list[dict[str, Any]], date_from: str | None, date_to: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Meetings held from `date_from` to `date_to` (inclusive, either end may be open), oldest first, plus a label.
    The label is None when no usable date was given; with a label and an empty list, no meeting falls in the range.
    """
    held = [(m, day) for m in meetings if (day := _meeting_day(m))]
    start = _resolve_day(date_from, [day for _, day in held])
    end = _resolve_day(date_to, [day for _, day in held])
    if not start and not end:
        return [], None
    if start and end and start > end:
        start, end = end, start
    matched = [m for m, day in held if (not start or day >= start) and (not end or day <= end)]
    if start and end:
        label = start.isoformat() if start == end else f"{start.isoformat()} to {end.isoformat()}"
    else:
        label = f"{start.isoformat()} onwards" if start else f"{end.isoformat()} and earlier"
    return sorted(matched, key=lambda m: str(m.get("date") or "")), label


# ============================================================================
# Node 1: Extract Filters Node
# ============================================================================

async def extract_filters_node(state: ChatState) -> dict[str, Any]:
    """
    Node 1: Extract Filters.
    Parses user query to identify mentioned participants from database metadata, checks if a specific meeting title or participant-based meeting scope is requested, and classifies query intent (catalog vs participant involvement vs content Q&A).
    """
    query, date_from, date_to = await rewrite_query(state.get("query", "").strip(), state.get("history") or [])
    user_id = state["user_id"]  # required: every query is scoped to one account
    project_id = state.get("project_id")
    resolved_speaker = state.get("extracted_speaker")
    resolved_speakers: list[str] = _state_speakers(state)

    resolved_meeting_id = state.get("meeting_id")
    date_meeting_ids: list[str] | None = None  # several meetings share the requested date
    no_meeting_on_date: str | None = None  # the requested date, when no meeting was held on it
    relevant_meetings: list[dict[str, Any]] = []
    available_meetings: list[dict[str, Any]] = []
    available_projects: list[dict[str, Any]] = []
    available_speakers: list[str] = []
    query_intent = classify_query_intent(query, resolved_speakers)
    reasoning = "Universal semantic vector search across meeting transcripts"
    project_tokens: set[str] = set()

    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            # 1. Fetch available meetings and projects from PostgreSQL
            if project_id:
                m_res = await session.execute(
                    text("SELECT id, title, project_id, date FROM meetings WHERE user_id = :u_id AND project_id = :p_id"),
                    {"u_id": user_id, "p_id": project_id},
                )
            else:
                m_res = await session.execute(
                    text("SELECT id, title, project_id, date FROM meetings WHERE user_id = :u_id"), {"u_id": user_id}
                )
            meetings_db = m_res.fetchall()
            meetings_dict = {m[0]: (m[1] or m[0]).strip() for m in meetings_db}
            available_meetings = [
                {"id": m[0], "title": (m[1] or m[0]).strip(), "project_id": _unscope_proj(m[2]), "date": format_meeting_date(m[3])}
                for m in meetings_db
            ]

            try:
                async with session.begin_nested():
                    p_res = await session.execute(
                        text("SELECT id, name, status FROM projects WHERE user_id = :u_id"), {"u_id": user_id}
                    )
                    available_projects = [
                        {"id": r[0], "name": (r[1] or _unscope_proj(r[0]) or r[0]).strip(), "status": (r[2] or "ACTIVE")}
                        for r in p_res.fetchall()
                    ]
            except Exception as e:
                logger.warning(f"[LANGGRAPH: EXTRACT_FILTERS] Could not query projects table: {e}")

            if not available_projects:
                try:
                    async with session.begin_nested():
                        m_p_res = await session.execute(
                            text("SELECT DISTINCT project_id FROM meetings WHERE user_id = :u_id AND project_id IS NOT NULL"),
                            {"u_id": user_id},
                        )
                        available_projects = [
                            {"id": r[0], "name": f"Project {_unscope_proj(r[0]) or r[0]}", "status": "ACTIVE"}
                            for r in m_p_res.fetchall()
                        ]
                except Exception:
                    pass

            # Dynamically derive project title tokens from database records
            db_project_tokens = {
                w.lower()
                for p in available_projects
                for w in re.findall(r"[a-zA-Z0-9_\-]+", p.get("id", "") + " " + p.get("name", ""))
                if len(w) > 2
            }
            project_tokens = set(db_project_tokens)
            if not project_id:
                q_words_lower = set(re.findall(r"[a-zA-Z0-9]+", query.lower()))
                for p in available_projects:
                    p_id = p.get("id", "").strip()
                    p_name = p.get("name", "").strip().lower()
                    unscoped = (_unscope_proj(p_id) or "").lower()
                    if (
                        (p_id and p_id.lower() in q_words_lower)
                        or (p_name and p_name in q_words_lower)
                        or (unscoped and unscoped in q_words_lower)
                    ):
                        project_id = p_id
                        break

            # 2. Dynamic Speaker Entity Extraction from database records
            matched_spk_phrase = None
            spk_res = await session.execute(
                text("SELECT DISTINCT COALESCE(metadata->>'speaker', speaker_name) FROM speaker_turns WHERE user_id = :u_id AND (speaker_name IS NOT NULL OR metadata->>'speaker' IS NOT NULL)"),
                {"u_id": user_id},
            )
            available_speakers = [r[0] for r in spk_res.fetchall() if r[0]]

            if not resolved_speakers:
                found_speakers = resolve_speakers_from_query(query, available_speakers, db_project_tokens)
                if found_speakers:
                    resolved_speakers = [s[0] for s in found_speakers]
                    resolved_speaker = resolved_speakers[0]
                    matched_spk_phrase = " ".join(s[1] for s in found_speakers)
                    for s in found_speakers:
                        logger.info(f"[LANGGRAPH: EXTRACT_FILTERS] {s[2]}")

            # 3. Dynamic Meeting Resolution
            m_db_pairs = [(m[0], m[1]) for m in meetings_db]
            if resolved_meeting_id:
                m_title = meetings_dict.get(resolved_meeting_id, resolved_meeting_id)
                reasoning = f"Scoped to meeting '{m_title}' ({resolved_meeting_id})"
            else:
                on_date, date_label = meetings_in_date_range(available_meetings, date_from, date_to)
                if len(on_date) == 1:
                    resolved_meeting_id = on_date[0]["id"]
                    reasoning = (
                        f"Scoped to the meeting held on {date_label}: '{on_date[0]['title']}' ({resolved_meeting_id})"
                    )
                elif on_date:
                    date_meeting_ids = [m["id"] for m in on_date]
                    reasoning = f"{len(on_date)} meetings were held on {date_label}; searching all of them"
                elif date_label:
                    no_meeting_on_date = date_label
                    reasoning = f"No meeting was held on {date_label}"
                else:
                    m_id, m_title, meeting_reasoning = resolve_meeting_from_query(
                        query, m_db_pairs, matched_spk_phrase, available_meetings=available_meetings
                    )
                    if m_id:
                        resolved_meeting_id = m_id
                        reasoning = meeting_reasoning

            # 4. Classify Query Intent (Generic Intent Classification)
            query_intent = classify_query_intent(query, resolved_speakers)

            # Check if query is asking for participant list / roster (typo-tolerant)
            q_lower = normalize_entity_typos(query).lower()
            is_participant_list = any(w in q_lower for w in ["participant", "participants", "participated", "attendee", "attendees", "attended", "speaker", "speakers"]) and any(w in q_lower for w in ["who", "list", "show", "all", "what", "which"])
            if is_participant_list and not resolved_speakers:
                query_intent = QueryIntent.CATALOG
                if project_id:
                    p_spk_res = await session.execute(
                        text("SELECT DISTINCT COALESCE(t.metadata->>'speaker', t.speaker_name) FROM speaker_turns t JOIN meetings m ON t.meeting_id = m.id WHERE t.user_id = :u_id AND m.user_id = :u_id AND m.project_id = :p_id AND (t.speaker_name IS NOT NULL OR t.metadata->>'speaker' IS NOT NULL)"),
                        {"u_id": user_id, "p_id": project_id}
                    )
                    available_speakers = [r[0] for r in p_spk_res.fetchall() if r[0]]
                    reasoning = f"Catalog query: Found {len(available_speakers)} participant(s) in project {project_id}"

            # 5. Intent-driven routing logic
            if query_intent == QueryIntent.PARTICIPANT_INVOLVEMENT and resolved_speakers:
                spk_clauses = []
                params = {"u_id": user_id}
                for idx, s in enumerate(resolved_speakers):
                    p_name = f"s_param_{idx}"
                    spk_clauses.append(_speaker_clause(p_name))
                    params[p_name] = f"%{s.lower()}%"

                spk_sql = (
                    "SELECT m.id, m.title, count(t.turn_id) as turn_count "
                    "FROM meetings m "
                    "JOIN speaker_turns t ON m.id = t.meeting_id "
                    f"WHERE m.user_id = :u_id AND t.user_id = :u_id AND ({' OR '.join(spk_clauses)}) "
                    "GROUP BY m.id, m.title "
                    "ORDER BY turn_count DESC"
                )
                m_spk_res = await session.execute(text(spk_sql), params)
                m_candidates = m_spk_res.fetchall()
                if m_candidates:
                    relevant_meetings = [
                        {"id": r[0], "title": (r[1] or r[0]).strip(), "turns": r[2]}
                        for r in m_candidates
                    ]
                    m_titles_summary = ", ".join(f"'{m['title']}'" for m in relevant_meetings)
                    reasoning = f"Found {len(relevant_meetings)} meeting(s) for participant(s) {', '.join(resolved_speakers)}: {m_titles_summary}"
                else:
                    reasoning = f"No meeting records found for participant(s): {', '.join(resolved_speakers)}"

            elif query_intent == QueryIntent.CATALOG:
                # Catalog answers always carry projects, meetings and participants (see build_catalog_records)
                ent_desc = (
                    f"{len(available_projects)} project(s), {len(available_meetings)} meeting(s), "
                    f"{len(available_speakers)} participant(s)"
                )
                if "Catalog query:" not in reasoning:
                    reasoning = f"Catalog query resolved against relational database: {ent_desc}"

            else:
                if resolved_speakers and not resolved_meeting_id:
                    reasoning = f"Universal semantic search filtered to participant(s): {', '.join(resolved_speakers)}"
                elif resolved_speakers and resolved_meeting_id:
                    reasoning += f" | Filtered to participant(s): {', '.join(resolved_speakers)}"

    logger.info(
        f"[LANGGRAPH: EXTRACT_FILTERS] Intent: {query_intent} | Speakers: {resolved_speakers} | "
        f"Meeting: '{resolved_meeting_id}' | Relevant Meetings: {len(relevant_meetings)} | Projects: {len(available_projects)} | Reasoning: '{reasoning}'"
    )

    # Only pure inventory questions are answered from the database alone. Anything that
    # mentions other words (decisions, risks, topics...) still gets transcript retrieval.
    skip_retrieval = query_intent == QueryIntent.CATALOG and is_pure_inventory_question(
        query, project_tokens
    ) and not re.search(r"\b(?:topics?|agenda items?)\b", normalize_entity_typos(query).lower())

    return {
        "query_intent": query_intent,
        "skip_retrieval": skip_retrieval,
        "extracted_speaker": resolved_speaker,
        "extracted_speakers": resolved_speakers,
        "query": query,  # corrected, standalone question used by every later step
        "meeting_id": resolved_meeting_id,
        "date_meeting_ids": date_meeting_ids,
        "no_meeting_on_date": no_meeting_on_date,
        "project_id": project_id,
        "available_meetings": available_meetings,
        "available_projects": available_projects,
        "available_speakers": available_speakers,
        "relevant_meetings": relevant_meetings,
        "reasoning": reasoning,
    }
