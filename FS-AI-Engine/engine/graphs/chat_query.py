"""
FS-AI-Engine/graphs/chat_query.py
=================================
Shared building blocks of the chat pipeline: the ChatState schema, query intent categories,
stop-word vocabularies, and the pure query-analysis helpers (typo normalisation, inventory
detection, intent classification, participant and meeting resolution).
"""

from __future__ import annotations

import re
from typing import Any, TypedDict

from .chat_entities import (  # noqa: F401  (re-exported)
    MEETING_STOP_WORDS,
    ORGANIZATIONAL_COMMON_WORDS,
    is_name_token_match,
    resolve_meeting_from_query,
    resolve_speakers_from_query,
)


class QueryIntent:
    """Semantic intent classification categories for conversational routing."""
    CATALOG = "catalog"                         # Schema/inventory queries (projects, meetings, participants, topics, statuses, counts)
    PARTICIPANT_INVOLVEMENT = "participant_involvement" # Which meetings a participant attended or took part in
    CONTENT_QA = "content_qa"                   # Substantive questions about meeting discussions, decisions, action items, or transcripts


# Words that decide whether a question is about the catalog (projects, meetings, people, topics).
ENTITY_VOCAB = (
    "meeting", "meetings", "project", "projects", "participant", "participants",
    "speaker", "speakers", "attendee", "attendees", "topic", "topics",
)
_MIN_TYPO_WORD_LEN = 5


def _within_one_edit(a: str, b: str) -> bool:
    """True if `a` and `b` differ by at most one insertion, deletion, substitution or adjacent swap."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diffs) == 1:
            return True
        return len(diffs) == 2 and diffs[1] == diffs[0] + 1 and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(short) and short[i] == long_[i]:
        i += 1
    return short[i:] == long_[i + 1:]


def normalize_entity_typos(query: str) -> str:
    """
    Corrects one-edit misspellings of the entity words that drive routing
    ("meeitngs" -> "meetings"). Conservative on purpose: only words of 5+ letters that are
    not already known, and only a single edit away, so ordinary words are left alone.
    """
    def fix(match: re.Match[str]) -> str:
        word = match.group(0)
        low = word.lower()
        if len(low) < _MIN_TYPO_WORD_LEN or low in ENTITY_VOCAB:
            return word
        candidates = [v for v in ENTITY_VOCAB if _within_one_edit(low, v)]
        return candidates[0] if len(candidates) == 1 else word

    return re.sub(r"[A-Za-z]+", fix, query)


# Words that may appear in a pure inventory question ("how many meetings are there under the project?").
_INVENTORY_WORDS = frozenset(["how", "many", "much", "count", "number", "total", "list", "show", "give", "tell", "me", "us", "enumerate", "display", "name", "names", "which", "what", "who", "whom", "all", "every", "each", "the", "a", "an", "any", "are", "is", "was", "were", "there", "be", "been", "in", "of", "on", "at", "under", "for", "to", "by", "with", "from", "into", "across", "our", "your", "my", "this", "that", "these", "those", "we", "you", "i", "do", "does", "did", "have", "has", "had", "can", "could", "please", "system", "database", "available", "existing", "registered", "active", "recorded", "currently", "so", "far", "overall", "ingested", "stored", "and", "or", "id", "ids", "title", "titles", "status", "statuses"]) | frozenset(ENTITY_VOCAB)


def is_pure_inventory_question(query: str, known_tokens: set[str] | None = None) -> bool:
    """
    True only when every word is inventory vocabulary (or a known project/meeting token).
    Any other word ("decisions", "ratified", "risks") means the question is about transcript
    content, so retrieval must still run. A wrong answer here only costs speed, never accuracy.
    """
    allowed = _INVENTORY_WORDS | (known_tokens or set())
    words = re.findall(r"[a-z0-9_\-]+", normalize_entity_typos(query).lower())
    return bool(words) and all(w in allowed for w in words)


def classify_query_intent(query: str, resolved_speakers: list[str]) -> str:
    """
    Classifies user query into a semantic pipeline intent:
    - CATALOG: Queries asking for schema/inventory of entities (projects, meetings, participants, topics, statuses, counts)
    - PARTICIPANT_INVOLVEMENT: Queries asking which meetings a participant attended or was involved in
    - CONTENT_QA: Substantive queries asking about meeting discussions, decisions, action items, or transcripts
    """
    q_lower = normalize_entity_typos(query).lower()

    # 1. Participant Involvement Intent
    involvement_signals = [
        "which meeting", "what meeting", "how many meeting", "all meeting",
        "list meeting", "attend", "attended", "present in", "participate",
        "involved in", "take part", "part of which", "part of", "joined"
    ]
    if resolved_speakers and any(sig in q_lower for sig in involvement_signals):
        return QueryIntent.PARTICIPANT_INVOLVEMENT
    # Structural rule: a person plus a question about *meetings* (not what was said) is about attendance
    if (
        resolved_speakers
        and re.search(r"\bmeetings?\b", q_lower)
        and not any(v in q_lower for v in ("say", "said", "discuss", "mention", "talk", "decid", "agree", "explain", "quote", "action item", "next step"))
    ):
        return QueryIntent.PARTICIPANT_INVOLVEMENT

    # 2. Catalog / Inventory / Schema Intent
    catalog_entity_patterns = [
        r"\b(?:projects?|active projects?)\b",
        r"\b(?:meetings?|available meetings?|recorded meetings?)\b",
        r"\b(?:participants?|speakers?|attendees?)\b",
        r"\b(?:topics?|agenda items?)\b",
    ]
    catalog_action_patterns = [
        r"\b(?:which|what|how many|count|list|show|enumerate|give me all|all the|all of the|available|existing|active|registered)\b"
    ]
    content_verbs = [
        "say", "said", "discuss", "discussed", "mention", "mentioned",
        "talk", "talked", "decide", "decided", "agree", "agreed",
        "explain", "explained", "quote", "action item", "next step"
    ]

    has_catalog_entity = any(re.search(p, q_lower) for p in catalog_entity_patterns)
    has_catalog_action = any(re.search(p, q_lower) for p in catalog_action_patterns)
    has_content_verb = any(v in q_lower for v in content_verbs)

    if has_catalog_entity and has_catalog_action and not has_content_verb and not resolved_speakers:
        return QueryIntent.CATALOG

    return QueryIntent.CONTENT_QA


class RewriteResult(tuple):
    """
    Structured outcome of the query rewrite step.
    Acts as a 4-tuple (query, date_from, date_to, follow_up) for full backwards-compatibility
    while exposing structured fields: speaker, time_scope, and intent.
    """
    query: str
    date_from: str | None
    date_to: str | None
    follow_up: bool
    speaker: str | None
    time_scope: str | None
    intent: str | None

    def __new__(
        cls,
        query: str,
        date_from: str | None,
        date_to: str | None,
        follow_up: bool,
        speaker: str | None = None,
        time_scope: str | None = None,
        intent: str | None = None,
    ):
        return super().__new__(cls, (query, date_from, date_to, follow_up))

    def __init__(
        self,
        query: str,
        date_from: str | None,
        date_to: str | None,
        follow_up: bool,
        speaker: str | None = None,
        time_scope: str | None = None,
        intent: str | None = None,
    ):
        self.query = query
        self.date_from = date_from
        self.date_to = date_to
        self.follow_up = follow_up
        self.speaker = speaker
        self.time_scope = time_scope
        self.intent = intent


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
    time_scope: str | None  # e.g. "latest" for "most recent meeting" / "last call"
    skip_retrieval: bool
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
