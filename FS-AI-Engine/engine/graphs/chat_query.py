"""
FS-AI-Engine/graphs/chat_query.py
=================================
Shared building blocks of the chat pipeline: the ChatState schema, query intent categories,
stop-word vocabularies, and the pure query-analysis helpers (typo normalisation, inventory
detection, intent classification, participant and meeting resolution).
"""

from __future__ import annotations

import difflib
import re
from typing import Any, TypedDict

# General English grammatical and conversational stop words
MEETING_STOP_WORDS = {
    "and", "or", "the", "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "about", "into", "through", "after", "before", "between", "under", "above",
    "all", "any", "both", "each", "few", "more", "most", "other", "some", "such",
    "no", "nor", "not", "only", "own", "same", "so", "than", "too", "very",
    "can", "will", "just", "should", "now", "what", "which", "who", "whom",
    "this", "that", "these", "those", "am", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "having", "do", "does", "did", "doing",
    "would", "could", "tell", "show", "give", "summarize", "summary", "notes",
    "point", "points", "key", "action", "item", "items", "decision", "decisions",
    "discussion", "discussions", "review", "sync", "call", "meeting", "meetings",
    "transcript", "transcripts", "overview", "details", "detail", "update", "updates",
    "hello", "hi", "hey", "direct", "test",
    "calls", "syncs", "reviews", "session", "sessions", "standup", "standups"
}


# Common organizational roles, titles, departments, and generic system nouns.
# In corporate meeting environments, these words denote roles/topics rather than unique individuals.
# Single-token matching is strictly forbidden on these words to prevent query hijacking.
ORGANIZATIONAL_COMMON_WORDS = {
    # System and generic placeholder terms
    "unknown", "speaker", "speakers", "system", "user", "users", "bot", "assistant",
    "participant", "participants", "member", "members", "team", "teams", "guest", "guests",
    "host", "hosts", "moderator", "scribe", "audience",
    # Professional titles and organizational roles
    "project", "projects", "manager", "managers", "management", "lead", "leads",
    "head", "director", "directors", "officer", "officers", "vp", "president",
    "engineer", "engineers", "engineering", "developer", "developers", "designer", "designers",
    "architect", "architects", "analyst", "analysts", "consultant", "consultants",
    "specialist", "specialists", "coordinator", "coordinators", "representative", "representatives",
    "rep", "reps", "executive", "executives", "tester", "testers", "qa", "admin", "administrator",
    "associate", "associates", "intern", "interns", "agent", "agents", "support",
    # Organizational functions, departments & business domains
    "product", "products", "security", "sales", "marketing", "finance", "legal",
    "operations", "devops", "hr", "account", "accounts", "billing", "cloud",
    # Entity nouns
    "meeting", "meetings", "call", "calls", "session", "sessions", "standup",
    "review", "discussion", "discussions", "sync", "topic", "topics", "agenda",
    "transcript", "transcripts", "item", "items", "action", "actions"
}


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


def is_name_token_match(token: str, candidate: str) -> bool:
    """Checks exact or fuzzy match for participant first/last names (e.g. dynamic name matching)."""
    t = token.lower()
    c = candidate.lower()
    if t == c:
        return True
    if len(t) >= 4 and len(c) >= 4:
        if difflib.SequenceMatcher(None, t, c).ratio() >= 0.85:
            return True
    return False


def resolve_speakers_from_query(
    query: str,
    speakers_db: list[str],
    project_tokens: set[str] | None = None,
) -> list[tuple[str, str, str]]:
    """
    Identifies participants dynamically and generically mentioned in the query.
    Distinguishes proper personal names from generic roles and titles:
    1. Full normalized name match against complete multi-token identifiers.
    2. Compound multi-word role/title phrase matching across adjacent tokens.
    3. Distinctive proper name token matching with common organizational terms filtered.
    Suppresses single-token false-positive matches on common organizational vocabulary,
    project identifiers, and entity container keywords.
    Returns a list of (resolved_speaker, matched_token, reasoning_log).
    """
    q_lower = query.lower()
    q_words = set(re.findall(r"[a-zA-Z0-9]+", q_lower))
    found_speakers: list[tuple[str, str, str]] = []
    seen = set()

    for spk in sorted(speakers_db, key=lambda s: len(s), reverse=True):
        spk_clean = spk.strip()
        if not spk_clean:
            continue
        # Remove trailing database generated UUIDs, Slack user IDs, or hash IDs if present
        clean_name = re.sub(r"\b[A-Z0-9_]{10,}\b", "", spk_clean).strip()
        clean_lower = clean_name.lower()

        if not clean_lower or clean_lower in ORGANIZATIONAL_COMMON_WORDS:
            continue

        # Strategy 1: Dynamic multi-word speaker full-name match
        if len(clean_lower) >= 4 and re.search(r"\b" + re.escape(clean_lower) + r"\b", q_lower):
            if spk_clean not in seen:
                seen.add(spk_clean)
                found_speakers.append((spk_clean, clean_lower, f"Detected participant full name '{spk_clean}'"))
            continue

        # Strategy 2: Multi-token role / compound phrase match (2 or 3 tokens)
        tokens = [p for p in re.findall(r"[a-zA-Z0-9]+", clean_name) if len(p) >= 2]
        if len(tokens) >= 2:
            matched_phrase = False
            for i in range(len(tokens) - 1):
                phrase = f"{tokens[i].lower()} {tokens[i+1].lower()}"
                if re.search(r"\b" + re.escape(phrase) + r"\b", q_lower):
                    if spk_clean not in seen:
                        seen.add(spk_clean)
                        found_speakers.append((spk_clean, phrase, f"Detected participant by role phrase '{phrase}' -> '{spk_clean}'"))
                    matched_phrase = True
                    break
            if matched_phrase:
                continue

        # Strategy 3: Distinctive proper name token match (filters out organizational common words & project tokens)
        distinctive_tokens = [
            t for t in tokens
            if len(t) >= 3
            and t.lower() not in ORGANIZATIONAL_COMMON_WORDS
            and not (project_tokens and t.lower() in project_tokens)
        ]
        for token in distinctive_tokens:
            matched = False
            for qw in q_words:
                # Generic Entity Guard: A word followed or preceded by "project" or "meeting" is an entity container, not a speaker
                if re.search(r"\b" + re.escape(qw) + r"\s+(?:projects?|meetings?)\b", q_lower) or \
                   re.search(r"\b(?:projects?|meetings?)\s+(?:named\s+|called\s+|for\s+)?(?:the\s+)?" + re.escape(qw) + r"\b", q_lower):
                    continue

                if is_name_token_match(qw, token):
                    if spk_clean not in seen:
                        seen.add(spk_clean)
                        found_speakers.append((spk_clean, qw, f"Detected participant proper name '{qw}' -> '{spk_clean}'"))
                    matched = True
                    break
            if matched:
                break

    return found_speakers


def resolve_meeting_from_query(
    query: str,
    meetings_db: list[tuple[str, str]],
    matched_speaker_phrase: str | None = None,
    available_meetings: list[dict[str, Any]] | None = None,
) -> tuple[str | None, str | None, str]:
    """
    Identifies if a specific meeting title or ID, or a relative reference such as "the last meeting",
    is mentioned in the query. A date ("June 12", "yesterday") is resolved by the caller, which can
    return several meetings.
    Returns (resolved_meeting_id, resolved_meeting_title, reasoning_log).
    """
    q_lower = query.lower()

    # If the user explicitly asks a cross-meeting, comparative, or inventory query, DO NOT lock to a single meeting
    cross_meeting_pattern = r"\b(?:across (?:all |the |our )?meetings|in all meetings|all (?:the )?meetings|which meetings|what meetings|list (?:all )?meetings|in any meeting|any meetings|meetings that|meetings where|every meeting)\b"
    if re.search(cross_meeting_pattern, q_lower):
        return None, None, "Cross-meeting query: searching across all meetings"

    # If a speaker phrase matched, mask it out when detecting meeting title
    q_for_meeting = q_lower
    if matched_speaker_phrase:
        q_masked = re.sub(r"\b" + re.escape(matched_speaker_phrase) + r"\b", " ", q_lower)
        if any(w for w in re.findall(r"\w+", q_masked) if w not in MEETING_STOP_WORDS):
            q_for_meeting = q_masked

    # 1. Exact ID match (case-insensitive)
    for m_id, m_title in meetings_db:
        if m_id and m_id.lower() in q_lower:
            m_title_clean = (m_title or m_id).strip()
            return m_id, m_title_clean, f"Scoped to meeting '{m_title_clean}' ({m_id})"

    # 2. Relative query resolution ("last meeting", "latest call", "previous sync"); dates are handled by the caller
    rel_pat = (
        r"\b(?:last|latest|most recent|recent|previous)\s+"
        r"(?:meeting|call|discussion|sync|session|standup|one)\b"
    )
    if available_meetings and re.search(rel_pat, q_for_meeting):
        dated = [m for m in available_meetings if m.get("date") and m["date"] != "not recorded"]
        pool = dated if dated else available_meetings
        if pool:
            sorted_pool = sorted(pool, key=lambda x: str(x.get("date") or ""), reverse=True)
            newest = sorted_pool[0]
            m_id = newest.get("id")
            m_title = (newest.get("title") or m_id or "").strip()
            return (
                m_id,
                m_title,
                f"Auto-scoped to most recent meeting '{m_title}' ({m_id}) based on relative temporal query",
            )

    best_score = 0
    best_m_id = None
    best_m_title = None

    for m_id, m_title in meetings_db:
        if not m_title:
            continue
        m_title_clean = m_title.strip()
        m_title_lower = m_title_clean.lower()

        score = 0
        norm_title = re.sub(r"[^a-zA-Z0-9\s]", " ", m_title_lower)
        norm_title = " ".join(norm_title.split())
        var_title = " ".join([w for w in norm_title.split() if w not in {"and", "&"}])

        # Whole title match has highest precedence
        if re.search(r"\b" + re.escape(norm_title) + r"\b", q_for_meeting):
            score += 200 + len(norm_title)
        elif len(var_title.split()) > 1 and re.search(r"\b" + re.escape(var_title) + r"\b", q_for_meeting):
            score += 150 + len(var_title)
        else:
            title_tokens = [w for w in re.findall(r"[a-zA-Z0-9_\-]+", m_title_lower) if w not in MEETING_STOP_WORDS]
            if title_tokens:
                matched_distinctive = [t for t in title_tokens if re.search(r"\b" + re.escape(t) + r"\b", q_for_meeting)]
                if matched_distinctive:
                    match_ratio = len(matched_distinctive) / len(title_tokens)
                    # Require at least 2 distinctive tokens or high coverage (>= 70%) to avoid hijacking by a single keyword
                    if len(matched_distinctive) >= 2 or match_ratio >= 0.7:
                        score = int(60 * match_ratio) + sum(len(t) for t in matched_distinctive)

        if score > best_score:
            best_score = score
            best_m_id = m_id
            best_m_title = m_title_clean

    if best_score >= 40:
        return best_m_id, best_m_title, f"Scoped to meeting '{best_m_title}' ({best_m_id})"

    return None, None, "Universal semantic vector search across meeting transcripts"


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
        "involved in", "take part", "part of which"
    ]
    if resolved_speakers and any(sig in q_lower for sig in involvement_signals):
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
    skip_retrieval: bool
    history: list[dict[str, str]]  # recent conversation turns, oldest first
    used_sources: list[int] | None  # excerpt numbers the LLM reports relying on (None = not reported)
    project_id: str | None
    meeting_id: str | None
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
