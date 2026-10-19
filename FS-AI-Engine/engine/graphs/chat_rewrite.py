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

4. "time_scope": "latest" if the question asks for the "last", "latest", "most recent", or "previous" meeting/discussion. Otherwise null.

5. "intent":
- "participant_involvement": User is asking which meeting(s) a person was in, attended, joined, took part in, or was part of (including their latest/most recent meeting).
- "catalog": User asks for rosters, inventories, counts, or lists of projects, meetings, participants, or topics in the system without asking for discussion contents (e.g. "how many meetings are there?", "list all projects").
- "content_qa": User asks about meeting content, discussions, decisions, what someone said, action items, or transcript details.

6. "date_from" and "date_to": Inclusive range of days (YYYY-MM-DD, or MM-DD if no year) on which meetings took place if explicitly named in the CURRENT message (never from history). Otherwise null.

Reply with only JSON:
{"query": "<question>", "follow_up": <true or false>, "speaker": "<name from Known Participants or null>", "time_scope": "<latest or null>", "intent": "<participant_involvement|catalog|content_qa>", "date_from": "<date or null>", "date_to": "<date or null>"}"""

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
) -> RewriteResult:
    """
    One small LLM call that fixes typos, resolves follow-ups, and extracts structured metadata:
    (query, date_from, date_to, follow_up, speaker, time_scope, intent).
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

    try:
        reply = await get_llm_client().generate_chat_completion(
            messages=[
                {"role": "system", "content": REWRITE_PROMPT},
                {
                    "role": "user",
                    "content": f"Today's date: {datetime.now(UTC):%Y-%m-%d}\n"
                    f"Conversation so far:\n{convo or '(none)'}{speaker_context}\n\nUser message: {query}",
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

        time_scope_val = parsed.get("time_scope")
        if time_scope_val and str(time_scope_val).lower().strip() in ("latest", "last", "recent", "most recent", "previous"):
            time_scope_val = "latest"
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
            speaker=None, time_scope=None, intent=None
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
        f"(dates: {date_from} to {date_to}, follow-up: {follow_up}, speaker: {speaker_val}, "
        f"scope: {time_scope_val}, intent: {intent_val})"
    )
    return RewriteResult(
        rewritten, date_from, date_to, follow_up,
        speaker=speaker_val, time_scope=time_scope_val, intent=intent_val
    )
