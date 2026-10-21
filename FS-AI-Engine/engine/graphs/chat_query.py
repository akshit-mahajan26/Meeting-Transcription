"""
FS-AI-Engine/graphs/chat_query.py
=================================
Shared building blocks of the chat pipeline: the ChatState schema, query intent categories,
and stop-word vocabularies. Query understanding (rewrite, intent, entities) is done by LLM prompts in
chat_rewrite.py and chat_analysis.py.
"""

from __future__ import annotations

from typing import Any, TypedDict

from .chat_entities import MEETING_STOP_WORDS, ORGANIZATIONAL_COMMON_WORDS, is_name_token_match  # noqa: F401  (re-exported)


class QueryIntent:
    """Semantic intent classification categories for conversational routing."""
    CATALOG = "catalog"                         # Schema/inventory queries (projects, meetings, participants, topics, statuses, counts)
    PARTICIPANT_INVOLVEMENT = "participant_involvement" # Which meetings a participant attended or took part in
    SMALLTALK = "smalltalk"                     # Greetings, thanks, goodbyes, "what can you do": answered directly, no lookup
    CONTENT_QA = "content_qa"                   # Substantive questions about meeting discussions, decisions, action items, or transcripts


# ============================================================================
# LangGraph State Schema
# ============================================================================

class ChatState(TypedDict, total=False):
    """
    Central state container tracked across all LangGraph nodes in the chat pipeline.
    """
    query: str
    user_id: str
    query_intent: str
    response_depth: str | None  # "concise" (direct factual lookup/short answer) or "explanatory" (deep dive, rationale, context, follow-up explanation)
    time_scope: str | None  # e.g. "latest" for "most recent meeting" / "last call"
    skip_retrieval: bool
    catalog_entities: list[str] | None  # database listings the question needs: projects, meetings, participants, topics
    history: list[dict[str, str]]  # recent conversation turns, oldest first
    used_sources: list[int] | None  # excerpt numbers the LLM reports relying on (None = not reported)
    project_id: str | None
    meeting_id: str | None
    meeting_scope_inferred: bool  # True when meeting_id/date_meeting_ids were guessed, False when the user picked the meeting
    date_meeting_ids: list[str] | None  # meetings sharing the date the question asks about
    no_meeting_on_date: str | None  # the requested date, when no meeting was held on it
    extracted_speaker: str | None
    extracted_speakers: list[str] | None
    query_embedding: list[float]
    retrieved_chunks: list[dict[str, Any]]
    relevant_meetings: list[dict[str, Any]] | None
    available_meetings: list[dict[str, Any]] | None
    available_projects: list[dict[str, Any]] | None
    available_speakers: list[str] | None
    answer: str
    citations: list[dict[str, Any]]
    citations_count: int
    provenance_verified: bool
    reasoning: str
    routing_mode: str
    llm_model_used: str
    latency_ms: float
    start_time: float


def _state_speakers(state: ChatState) -> list[str]:
    """Resolved participant names carried in the graph state (plural list, or the single legacy field)."""
    speaker = state.get("extracted_speaker")
    return state.get("extracted_speakers") or ([speaker] if speaker else [])


def _speaker_clause(param: str) -> str:
    """SQL fragment matching a speaker (metadata or column) against a LIKE parameter."""
    return f"(t.metadata->>'speaker' ILIKE :{param} OR LOWER(t.speaker_name) LIKE :{param})"
