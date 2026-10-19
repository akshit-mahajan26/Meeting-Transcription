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
from .chat_records import (
    _unscope_proj,
    build_catalog_records,
    format_meeting_date,
    meetings_in_date_range,
)
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


# ============================================================================
# Node 1: Extract Filters Node
# ============================================================================

async def extract_filters_node(state: ChatState) -> dict[str, Any]:
    """
    Node 1: Extract Filters.
    Parses user query to identify mentioned participants from database metadata, checks if a specific meeting title or participant-based meeting scope is requested, and classifies query intent (catalog vs participant involvement vs content Q&A).
    """
    user_id = state["user_id"]  # required: every query is scoped to one account
    project_id = state.get("project_id")
    resolved_speaker = state.get("extracted_speaker")
    resolved_speakers: list[str] = _state_speakers(state)

    resolved_meeting_id = state.get("meeting_id")
    user_picked_meeting = bool(resolved_meeting_id)
    date_meeting_ids: list[str] | None = None  # several meetings share the requested date
    no_meeting_on_date: str | None = None  # the requested date, when no meeting was held on it
    relevant_meetings: list[dict[str, Any]] = []
    available_meetings: list[dict[str, Any]] = []
    available_projects: list[dict[str, Any]] = []
    available_speakers: list[str] = []
    reasoning = "Universal semantic vector search across meeting transcripts"
    project_tokens: set[str] = set()
    time_scope: str | None = state.get("time_scope")
    cross_meeting = False
    raw_query = state.get("query", "").strip()

    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            # 1. Fetch available meetings, projects, and speakers from PostgreSQL first
            if project_id:
                m_res = await session.execute(
                    text("SELECT id, title, project_id, date FROM meetings WHERE user_id = :u_id AND project_id = :p_id AND status <> 'FAILED'"),
                    {"u_id": user_id, "p_id": project_id},
                )
            else:
                m_res = await session.execute(
                    text("SELECT id, title, project_id, date FROM meetings WHERE user_id = :u_id AND status <> 'FAILED'"), {"u_id": user_id}
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
                            text("SELECT DISTINCT project_id FROM meetings WHERE user_id = :u_id AND project_id IS NOT NULL AND status <> 'FAILED'"),
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

            # Dynamic Speaker List from database records
            spk_res = await session.execute(
                text("SELECT DISTINCT COALESCE(metadata->>'speaker', speaker_name) FROM speaker_turns WHERE user_id = :u_id AND (speaker_name IS NOT NULL OR metadata->>'speaker' IS NOT NULL)"),
                {"u_id": user_id},
            )
            available_speakers = [r[0] for r in spk_res.fetchall() if r[0]]

            # 2. Structured query rewrite with known participant context and available meetings
            rewrite_res = await rewrite_query(
                raw_query,
                state.get("history") or [],
                available_speakers=available_speakers,
                available_meetings=available_meetings,
            )
            query, date_from, date_to, _follow_up = rewrite_res[:4]
            structured_speaker = getattr(rewrite_res, "speaker", None)
            structured_time_scope = getattr(rewrite_res, "time_scope", None)
            structured_intent = getattr(rewrite_res, "intent", None)
            structured_meeting_id = getattr(rewrite_res, "meeting_id", None)
            cross_meeting = getattr(rewrite_res, "cross_meeting", False)
            if structured_time_scope:
                time_scope = structured_time_scope

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

            # 3. Dynamic Speaker Entity Extraction (Structured rewrite first, fallback to token matching)
            matched_spk_phrase = None
            if not resolved_speakers and structured_speaker:
                resolved_speakers = [structured_speaker]
                resolved_speaker = structured_speaker
                matched_spk_phrase = structured_speaker

            # Regex name matching is only a fallback for when the rewrite call failed; when the model answered
            # (even "no speaker"), its answer stands.
            if not resolved_speakers and structured_intent is None:
                title_tokens = {w.lower() for t in meetings_dict.values() for w in re.findall(r"[a-zA-Z0-9]+", t)}
                found_speakers = resolve_speakers_from_query(query, available_speakers, db_project_tokens | title_tokens)
                if found_speakers:
                    resolved_speakers = [s[0] for s in found_speakers]
                    resolved_speaker = resolved_speakers[0]
                    matched_spk_phrase = " ".join(s[1] for s in found_speakers)
                    for s in found_speakers:
                        logger.info(f"[LANGGRAPH: EXTRACT_FILTERS] {s[2]}")

            # 4. Dynamic Meeting Resolution
            m_db_pairs = [(m[0], m[1]) for m in meetings_db]
            if resolved_meeting_id:
                m_title = meetings_dict.get(resolved_meeting_id, resolved_meeting_id)
                reasoning = f"Scoped to meeting '{m_title}' ({resolved_meeting_id})"
            elif structured_meeting_id and structured_meeting_id in meetings_dict:
                resolved_meeting_id = structured_meeting_id
                m_title = meetings_dict[resolved_meeting_id]
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
                    rel_pool = available_meetings
                    if resolved_speakers:
                        clauses = [_speaker_clause(f"rs_{i}") for i in range(len(resolved_speakers))]
                        rs_params = {"u_id": user_id} | {f"rs_{i}": f"%{s.lower()}%" for i, s in enumerate(resolved_speakers)}
                        rs_res = await session.execute(
                            text(f"SELECT DISTINCT t.meeting_id FROM speaker_turns t WHERE t.user_id = :u_id AND ({' OR '.join(clauses)})"),
                            rs_params,
                        )
                        spoke_in = {r[0] for r in rs_res.fetchall()}
                        if spoke_in:
                            rel_pool = [m for m in available_meetings if m["id"] in spoke_in]
                    if cross_meeting:
                        m_id, m_title, meeting_reasoning = None, None, "Cross-meeting query: searching across all meetings"
                    else:
                        m_id, m_title, meeting_reasoning = resolve_meeting_from_query(
                            query, m_db_pairs, matched_spk_phrase
                        )
                    if not m_id and not cross_meeting and time_scope == "latest" and rel_pool:
                        newest = max(rel_pool, key=lambda m: str(m.get("date") or ""))
                        m_id, meeting_reasoning = newest["id"], f"Most recent meeting: '{newest['title']}' ({newest['id']})"
                    elif not m_id and not cross_meeting and time_scope == "earliest" and rel_pool:
                        earliest = min(rel_pool, key=lambda m: str(m.get("date") or ""))
                        m_id, meeting_reasoning = earliest["id"], f"Earliest meeting: '{earliest['title']}' ({earliest['id']})"
                    if m_id:
                        resolved_meeting_id = m_id
                        reasoning = meeting_reasoning

            # 5. Classify Query Intent (Structured from rewrite or fallback classifier)
            query_intent = structured_intent or classify_query_intent(query, resolved_speakers)

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

            # 6. Intent-driven routing logic
            if query_intent == QueryIntent.PARTICIPANT_INVOLVEMENT and resolved_speakers:
                spk_clauses = []
                params = {"u_id": user_id}
                for idx, s in enumerate(resolved_speakers):
                    p_name = f"s_param_{idx}"
                    spk_clauses.append(_speaker_clause(p_name))
                    params[p_name] = f"%{s.lower()}%"

                spk_sql = (
                    "SELECT m.id, m.title, m.date, count(t.turn_id) as turn_count "
                    "FROM meetings m "
                    "JOIN speaker_turns t ON m.id = t.meeting_id "
                    f"WHERE m.user_id = :u_id AND t.user_id = :u_id AND ({' OR '.join(spk_clauses)}) "
                    "GROUP BY m.id, m.title, m.date "
                    "ORDER BY m.date DESC NULLS LAST"
                )
                m_spk_res = await session.execute(text(spk_sql), params)
                m_candidates = m_spk_res.fetchall()
                if m_candidates:
                    relevant_meetings = [
                        {"id": r[0], "title": (r[1] or r[0]).strip(), "date": format_meeting_date(r[2]), "turns": r[3]}
                        for r in m_candidates
                    ]
                    if time_scope == "latest":
                        latest_m = relevant_meetings[0]
                        resolved_meeting_id = latest_m["id"]
                        relevant_meetings = [latest_m]
                        reasoning = f"Most recent meeting for participant(s) {', '.join(resolved_speakers)}: '{latest_m['title']}' ({latest_m['id']}) on {latest_m.get('date', 'not recorded')}"
                    elif time_scope == "earliest":
                        earliest_m = relevant_meetings[-1]
                        resolved_meeting_id = earliest_m["id"]
                        relevant_meetings = [earliest_m]
                        reasoning = f"Earliest meeting for participant(s) {', '.join(resolved_speakers)}: '{earliest_m['title']}' ({earliest_m['id']}) on {earliest_m.get('date', 'not recorded')}"
                    else:
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

    else:
        # Fallback when running without a database session
        rewrite_res = await rewrite_query(
            raw_query,
            state.get("history") or [],
            available_meetings=state.get("available_meetings") or [],
            available_speakers=state.get("available_speakers") or [],
        )
        query, date_from, date_to, _follow_up = rewrite_res[:4]
        structured_speaker = getattr(rewrite_res, "speaker", None)
        structured_time_scope = getattr(rewrite_res, "time_scope", None)
        structured_intent = getattr(rewrite_res, "intent", None)
        structured_meeting_id = getattr(rewrite_res, "meeting_id", None)
        if structured_time_scope:
            time_scope = structured_time_scope
        if structured_speaker and not resolved_speakers:
            resolved_speakers = [structured_speaker]
            resolved_speaker = structured_speaker
        if structured_meeting_id and not resolved_meeting_id and structured_meeting_id in {m.get("id") for m in state.get("available_meetings") or []}:
            resolved_meeting_id = structured_meeting_id
            reasoning = f"Scoped to meeting '{resolved_meeting_id}'"
        query_intent = structured_intent or classify_query_intent(query, resolved_speakers)

    logger.info(
        f"[LANGGRAPH: EXTRACT_FILTERS] Intent: {query_intent} | Speakers: {resolved_speakers} | "
        f"Meeting: '{resolved_meeting_id}' | Relevant Meetings: {len(relevant_meetings)} | Projects: {len(available_projects)} | Reasoning: '{reasoning}'"
    )

    # Attendance/involvement questions and pure inventory questions are answered strictly from the
    # relational database records (speaker_turns / meetings), never from transcript vector search
    # (which can confuse mentions of absence with actual attendance).
    is_pure_attendance = query_intent == QueryIntent.PARTICIPANT_INVOLVEMENT and bool(relevant_meetings) and not any(
        v in query.lower()
        for v in ("say", "said", "discuss", "mention", "talk", "decid", "agree", "explain", "quote", "action item", "next step", "what did")
    )
    skip_retrieval = (
        (query_intent == QueryIntent.CATALOG and is_pure_inventory_question(
            query, project_tokens
        ) and not re.search(r"\b(?:topics?|agenda items?)\b", normalize_entity_typos(query).lower()))
        or is_pure_attendance
    )

    return {
        "query_intent": query_intent,
        "time_scope": time_scope,
        "skip_retrieval": skip_retrieval,
        "extracted_speaker": resolved_speaker,
        "extracted_speakers": resolved_speakers,
        "query": query,  # corrected, standalone question used by every later step
        # Preserve conversation history so synthesis LLM has context for references and follow-ups
        "history": state.get("history") or [],
        "meeting_id": resolved_meeting_id,
        "meeting_scope_inferred": not user_picked_meeting and bool(resolved_meeting_id or date_meeting_ids),
        "date_meeting_ids": date_meeting_ids,
        "no_meeting_on_date": no_meeting_on_date,
        "project_id": project_id,
        "available_meetings": available_meetings,
        "available_projects": available_projects,
        "available_speakers": available_speakers,
        "relevant_meetings": relevant_meetings,
        "reasoning": reasoning,
    }
