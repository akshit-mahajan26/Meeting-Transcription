"""
FS-AI-Engine/graphs/chat_entities.py
====================================
Name and title matching used as the fallback when the rewrite step cannot resolve entities:
stop-word vocabularies, fuzzy name matching, participant and meeting resolution.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

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
