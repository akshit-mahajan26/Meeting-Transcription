"""
FS-AI-Engine/graphs/chat_rewrite.py
===================================
The query-rewrite step: one small LLM call that fixes typos, resolves follow-ups and returns the
structured speaker / time scope / intent / date range that routes the rest of the pipeline.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from ..core.config import settings
from ..services.llm_client import get_llm_client
from .chat_query import QueryIntent, RewriteResult, is_name_token_match

logger = logging.getLogger("fs-ai-engine.chat_graph")

REWRITE_PROMPT = """You are the intent, entity, and question rewrite parser for a meeting-transcript assistant.
Analyze the user's message and output a clean JSON object:

1. "query": Rewrite the user's message as a clear, standalone question.
- Fix typos and word errors using the sentence context (e.g. "rate of the meeting" -> "date of the meeting").
- If "follow_up" is true, make the question standalone by resolving pronouns and back-references ("he", "she", "it", "they", "that one", "his last meeting", "why?") using the earlier conversation.
- If it is already clear or standalone, keep it unchanged or clean it up. Never answer the question or invent facts.
2. "follow_up": True ONLY if the message cannot be understood without the earlier conversation (refers back to prior context with "he", "she", "it", "they", "those", "that", "his", "their", "the other one"). False if self-contained.
3. "speaker": If the question refers to or asks about a specific person (by name, or via a pronoun like "he", "she", "his" resolved from earlier conversation), select their EXACT full name from the Known Participants list below. If the person is not in the list or no specific person is referred to, return null.
4. "meeting_id": If the question refers to a specific meeting—whether by title, calendar date, relative/ordinal ordering (e.g. "the first meeting", "second call", "latest standup", "the meeting after X"), or pronouns referring to a prior meeting—select its exact ID from the Available Meetings list below. If the question is cross-meeting (e.g. "all meetings", "each meeting", "every meeting", "any meeting", "in which meetings"), asks about multiple meetings, or does not target a specific meeting, return null. Set "cross_meeting" to true in the cross-meeting or multi-meeting case, otherwise false.
5. "time_scope": "latest" if the question asks for the "last", "latest", "most recent", or "previous" meeting/discussion. "earliest" if it asks for the "first", "initial", "earliest", or "oldest" meeting. Otherwise null.
6. "intent":
- "participant_involvement": User is asking which meeting(s) a person was in, attended, joined, took part in, or was part of (including their latest/most recent meeting).
- "catalog": User asks for rosters, inventories, counts, or lists of projects, meetings, participants, or topics in the system without asking for discussion contents (e.g. "how many meetings are there?", "list all projects").
- "content_qa": User asks about meeting content, discussions, decisions, what someone said, action items, or transcript details.
7. "date_from" and "date_to": Inclusive range of days (YYYY-MM-DD, or MM-DD if no year) on which meetings took place if explicitly named in the CURRENT message (never from history). Otherwise null.
Reply with only JSON:
{"query": "<question>", "follow_up": <true or false>, "speaker": "<name from Known Participants or null>", "meeting_id": "<id from Available Meetings or null>", "cross_meeting": <true or false>, "time_scope": "<latest|earliest|null>", "intent": "<participant_involvement|catalog|content_qa>", "date_from": "<date or null>", "date_to": "<date or null>"}"""

_FOLLOW_UP_HINT = re.compile(
    r"^(and|also|but|so|then|why|how come|what about|how about)\b|\b(it|its|they|them|their|that|those|these|he|she|him|her|this one|the same|the other)\b",
    re.IGNORECASE,
)


_DATE_HINT = re.compile(
    r"\d{1,4}[-/.]\d{1,2}|\b\d{1,2}(st|nd|rd|th)?\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|"
    r"\b(today|yesterday|tomorrow|tonight|week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"before|after|since|between|until|recent|latest|last|previous|ago)\b",
    re.IGNORECASE,
)


def _mentions_date(query: str) -> bool:
    """True if the user's own message names a date or period. Dates found only in earlier turns do not count."""
    return bool(_DATE_HINT.search(query))


def _looks_like_follow_up(query: str) -> bool:
    """Cheap guess used only when the rewrite call fails: short messages with back-references."""
    return len(query.split()) <= 8 and bool(_FOLLOW_UP_HINT.search(query))


async def rewrite_query(
    query: str,
    history: list[dict[str, str]],
    available_speakers: list[str] | None = None,
    available_meetings: list[dict[str, Any]] | None = None,
) -> RewriteResult:
    """
    One small LLM call that fixes typos, resolves follow-ups, and extracts structured metadata:
    (query, date_from, date_to, follow_up, speaker, time_scope, intent, meeting_id).
    `follow_up` indicates whether earlier conversation was required to understand the message.
    """
    if not history:
        history = []
    convo_lines = []
    # Only the user's own earlier questions: assistant answers can carry wrong names, titles and dates into the rewrite
    for m in [m for m in history if m.get("role") == "user"][-3:]:
        content = (m.get("content") or "").strip()
        if content:
            convo_lines.append(f"user: {content[:400]}")
    convo = "\n".join(convo_lines)

    speaker_context = ""
    if available_speakers:
        spk_list_str = ", ".join(f'"{s}"' for s in available_speakers if s)
        speaker_context = f"\nKnown Participants: [{spk_list_str}]"

    meeting_context = ""
    valid_meeting_ids: set[str] = set()
    if available_meetings:
        sorted_m = sorted(
            [m for m in available_meetings if m.get("id")],
            key=lambda m: str(m.get("date") or "")
        )
        valid_meeting_ids = {m["id"] for m in sorted_m}
        mtg_lines = [
            f'- "{m["id"]}": "{m.get("title") or m["id"]}" ({m.get("date") or "not recorded"})'
            for m in sorted_m[-50:]
        ]
        if mtg_lines:
            meeting_context = "\nAvailable Meetings (chronological order from earliest to latest; the most recent 50 are shown):\n" + "\n".join(mtg_lines)

    try:
        reply = await get_llm_client().generate_chat_completion(
            messages=[
                {"role": "system", "content": REWRITE_PROMPT},
                {
                    "role": "user",
                    "content": f"Today's date: {datetime.now(UTC):%Y-%m-%d}\n"
                    f"Conversation so far:\n{convo or '(none)'}{speaker_context}{meeting_context}\n\nUser message: {query}",
                },
            ],
            model=settings.LLM_MODEL_NAME,
            max_tokens=250,
            temperature=0.0,
        )
        parsed = json.loads(re.search(r"\{.*\}", reply, re.DOTALL).group(0))
        rewritten = str(parsed.get("query") or "").strip()
        date_from, date_to = (str(parsed[k]) if parsed.get(k) else None for k in ("date_from", "date_to"))
        # The model's flag alone is not trusted: a question is a follow-up only if it also contains a back-reference
        follow_up = bool(history) and parsed.get("follow_up") is True and bool(_FOLLOW_UP_HINT.search(query))
        if not _mentions_date(query):  # the model may pick dates up from earlier answers; ignore them
            date_from = date_to = None

        speaker_val = parsed.get("speaker")
        if speaker_val:
            speaker_str = str(speaker_val).strip()
            if available_speakers:
                spk_match = [
                    s for s in available_speakers
                    if s.lower() == speaker_str.lower()
                    or is_name_token_match(speaker_str, s)
                    or speaker_str.lower() in s.lower()
                ]
                speaker_val = spk_match[0] if spk_match else None  # a name not in the participant list is dropped
            else:
                speaker_val = None
        else:
            speaker_val = None

        meeting_id_val = parsed.get("meeting_id")
        if meeting_id_val and available_meetings:
            mid_str = str(meeting_id_val).strip()
            if mid_str in valid_meeting_ids:
                meeting_id_val = mid_str
            else:
                needle = mid_str.lower()
                exact = [m["id"] for m in available_meetings if m.get("id") and (m.get("title") or "").strip().lower() == needle]
                partial = [m["id"] for m in available_meetings if m.get("id") and needle in (m.get("title") or "").lower()]
                matches = exact or partial
                meeting_id_val = matches[0] if len(matches) == 1 else None
        else:
            meeting_id_val = None

        cross_meeting_val = parsed.get("cross_meeting") is True
        if cross_meeting_val:
            meeting_id_val = None

        time_scope_val = parsed.get("time_scope")
        if time_scope_val:
            ts_clean = str(time_scope_val).lower().strip()
            if ts_clean in ("latest", "last", "recent", "most recent", "previous"):
                time_scope_val = "latest"
            elif ts_clean in ("earliest", "first", "initial", "oldest"):
                time_scope_val = "earliest"
            else:
                time_scope_val = None
        else:
            time_scope_val = None

        intent_val = parsed.get("intent")
        if intent_val:
            intent_clean = str(intent_val).lower().strip()
            if intent_clean in (QueryIntent.PARTICIPANT_INVOLVEMENT, QueryIntent.CATALOG, QueryIntent.CONTENT_QA):
                intent_val = intent_clean
            else:
                intent_val = None
        else:
            intent_val = None

    except Exception as exc:  # network error, no JSON in the reply, ...
        logger.warning(f"[LANGGRAPH: REWRITE_QUERY] Keeping the original query: {exc}")
        return RewriteResult(
            query, None, None, bool(history) and _looks_like_follow_up(query),
            speaker=None, time_scope=None, intent=None, meeting_id=None
        )

    if not rewritten or len(rewritten) > 4 * len(query) + 200:
        rewritten = query
    elif history and not follow_up:
        # A standalone question may be tidied (typos) but must not pick up names or titles from earlier answers
        added = set(re.findall(r"\w+", rewritten.lower())) - set(re.findall(r"\w+", query.lower()))
        if len(added) > 2:
            rewritten = query

    logger.info(
        f"[LANGGRAPH: REWRITE_QUERY] '{query}' -> '{rewritten}' "
        f"(meeting: {meeting_id_val}, dates: {date_from} to {date_to}, follow-up: {follow_up}, speaker: {speaker_val}, "
        f"scope: {time_scope_val}, intent: {intent_val}, cross-meeting: {cross_meeting_val})"
    )
    return RewriteResult(
        rewritten, date_from, date_to, follow_up,
        speaker=speaker_val, time_scope=time_scope_val, intent=intent_val, meeting_id=meeting_id_val,
        cross_meeting=cross_meeting_val,
    )
