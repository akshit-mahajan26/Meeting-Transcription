"""
FS-AI-Engine/graphs/chat_entities.py
====================================
Stop-word vocabularies and fuzzy name matching, used to validate participant names and to match transcript text.
"""

from __future__ import annotations

import difflib

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
