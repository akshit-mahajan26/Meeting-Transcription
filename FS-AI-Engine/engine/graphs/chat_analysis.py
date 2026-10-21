"""
FS-AI-Engine/graphs/chat_analysis.py
====================================
The query-analysis step: one LLM call, governed by a single system prompt holding every routing rule, that turns
the standalone question into the structured intent, entities, time scope and date range that route the pipeline.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..core.config import settings
from ..services.llm_client import get_llm_client
from .chat_entities import is_name_token_match
from .chat_query import QueryIntent

logger = logging.getLogger("fs-ai-engine.chat_graph")

CATALOG_ENTITIES = ("projects", "meetings", "participants", "topics")

ANALYSIS_PROMPT = """You are the routing analyst for a meeting-transcript assistant. You receive one standalone question plus the \
Known Participants, Available Projects and Available Meetings, and you decide how the question is handled. Judge by meaning, \
never by exact words: the question may contain typos or unusual phrasing.

Output one JSON object with these fields:

1. "intent" (required), one of:
- "smalltalk": the WHOLE message is a greeting, thanks, goodbye, pleasantry, or a question about the assistant itself ("hello", "hey there", "thanks!", "good morning", "how are you", "who are you", "what can you do"). If it also asks anything about meetings, people, projects or content ("hello, what did Sam say?"), it is NOT smalltalk.
- "participant_involvement": which meeting(s) a person was in, attended, joined, took part in or was part of, including their latest or earliest meeting. Needs a specific person.
- "catalog": rosters, inventories, counts or lists of projects, meetings, participants or topics in the system, with no question about what was said ("how many meetings are there?", "list all projects", "who are the participants?").
- "content_qa": anything about meeting content: discussions, decisions, what someone said, action items, transcript details, summaries. This is the default when unsure.
2. "response_depth": "explanatory" ONLY when the user explicitly requests an in-depth breakdown or detailed explanation ("in detail", "detailed explanation", "explain in detail", "deep dive") OR when asking a follow-up to unpack, explain, or elaborate on a previous answer/topic ("can you explain it", "explain", "why is that?", "tell me more"). Otherwise "concise": the DEFAULT for all standard inquiries, initial questions, direct factual lookups, overviews, summaries, single numbers, dates, rosters, and general topic questions ("what was discussed", "tell me about", "why did X happen", "how does X work", "summarize the meeting").
3. "speaker": if the question is about a specific person, their EXACT full name from Known Participants; otherwise null. A name not in the list is null.
4. "meeting_id": if the question targets one specific meeting by title, calendar date, order ("the first meeting", "second call", "latest standup", "the meeting after X"), copy its exact ID from Available Meetings; otherwise null.
5. "cross_meeting": true if the question spans several or all meetings ("all meetings", "each meeting", "in which meetings", "every call"); then "meeting_id" must be null. Otherwise false.
6. "time_scope": "latest" for the last, latest, most recent or previous meeting/discussion; "earliest" for the first, initial, earliest or oldest; otherwise null.
7. "date_from" / "date_to": the inclusive range of days (YYYY-MM-DD, or MM-DD if no year) on which the meetings in question took place, ONLY if the question names a date or period ("on 5 May", "after Oct 2", "last week", "yesterday"; work these out from Today's date). Otherwise null.
8. "project_id": if the question names one of the Available Projects (by name or ID), copy its exact ID; otherwise null.
9. "catalog_entities": which database listings are needed to answer, any of "projects", "meetings", "participants", "topics". Use [] when none are needed. For intent "catalog" list every entity the question concerns.
10. "needs_retrieval": true if the answer needs transcript text. false ONLY when it can be answered entirely from the database listings (names, counts, dates, who attended which meeting), i.e. a pure inventory or attendance question. Anything about what was said, decided, discussed, agreed, decided, topics or agenda, risks, owners or action items is true.
11. "reply": only for intent "smalltalk": a short, friendly reply (1-2 sentences) that fits the message and, where natural, mentions that you can answer questions about recorded meetings (discussions, decisions, action items, attendance, projects). Otherwise null.

Reply with only JSON:
{"intent": "<smalltalk|participant_involvement|catalog|content_qa>", "response_depth": "<concise|explanatory>", "speaker": "<name or null>", "meeting_id": "<id or null>", "cross_meeting": <true|false>, "project_id": "<id or null>", "time_scope": "<latest|earliest|null>", "date_from": "<date or null>", "date_to": "<date or null>", "catalog_entities": [<entities>], "needs_retrieval": <true|false>, "reply": "<text or null>"}"""


@dataclass
class QueryAnalysis:
    """Structured routing decision. The defaults describe the safe path: full transcript retrieval."""
    intent: str = QueryIntent.CONTENT_QA
    response_depth: str = "concise"
    speaker: str | None = None
    meeting_id: str | None = None
    project_id: str | None = None
    cross_meeting: bool = False
    time_scope: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    catalog_entities: list[str] = field(default_factory=list)
    needs_retrieval: bool = True
    reply: str | None = None


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return None if text.lower() in ("", "null", "none") else text


async def analyze_query(
    query: str,
    available_speakers: list[str] | None = None,
    available_meetings: list[dict[str, Any]] | None = None,
    available_projects: list[dict[str, Any]] | None = None,
) -> QueryAnalysis:
    """
    Classifies the question and extracts its entities. Anything the model returns that is not in the supplied
    lists (a made-up participant or meeting ID) is dropped. If the call fails, the default analysis applies
    (content question, full retrieval), which is always safe.
    """
    context = ""
    if available_speakers:
        context += "\nKnown Participants: [" + ", ".join(f'"{s}"' for s in available_speakers if s) + "]"
    if available_projects:
        context += "\nAvailable Projects (ID: name): [" + ", ".join(f'"{p["id"]}": "{p.get("name") or p["id"]}"' for p in available_projects if p.get("id")) + "]"

    valid_meeting_ids: set[str] = set()
    if available_meetings:
        ordered = sorted((m for m in available_meetings if m.get("id")), key=lambda m: str(m.get("date") or ""))
        valid_meeting_ids = {m["id"] for m in ordered}
        lines = [f'- "{m["id"]}": "{m.get("title") or m["id"]}" ({m.get("date") or "not recorded"})' for m in ordered[-50:]]
        if lines:
            context += "\nAvailable Meetings (chronological, earliest to latest; the most recent 50 are shown):\n" + "\n".join(lines)

    try:
        reply = await get_llm_client().generate_chat_completion(
            messages=[
                {"role": "system", "content": ANALYSIS_PROMPT},
                {"role": "user", "content": f"Today's date: {datetime.now(UTC):%Y-%m-%d}{context}\n\nQuestion: {query}"},
            ],
            model=settings.LLM_MODEL_NAME,
            max_tokens=350,
            temperature=0.0,
        )
        parsed = json.loads(re.search(r"\{.*\}", reply, re.DOTALL).group(0))
    except Exception as exc:  # network error, no JSON in the reply, ...
        logger.warning(f"[LANGGRAPH: ANALYZE_QUERY] Using the default analysis: {exc}")
        return QueryAnalysis()

    intent = str(parsed.get("intent") or "").lower().strip()
    if intent not in (QueryIntent.SMALLTALK, QueryIntent.PARTICIPANT_INVOLVEMENT, QueryIntent.CATALOG, QueryIntent.CONTENT_QA):
        intent = QueryIntent.CONTENT_QA

    if intent == QueryIntent.SMALLTALK:
        return QueryAnalysis(intent=intent, needs_retrieval=False, reply=_clean(parsed.get("reply")))

    speaker = _clean(parsed.get("speaker"))
    if speaker:
        matches = [
            s for s in (available_speakers or [])
            if s.lower() == speaker.lower() or is_name_token_match(speaker, s) or speaker.lower() in s.lower()
        ]
        speaker = matches[0] if matches else None  # a name not in the participant list is dropped

    cross_meeting = parsed.get("cross_meeting") is True
    meeting_id = _clean(parsed.get("meeting_id"))
    if meeting_id and meeting_id not in valid_meeting_ids:
        needle = meeting_id.lower()
        by_title = [m["id"] for m in available_meetings or [] if m.get("id") and (m.get("title") or "").strip().lower() == needle]
        meeting_id = by_title[0] if len(by_title) == 1 else None
    if cross_meeting:
        meeting_id = None

    project_id = _clean(parsed.get("project_id"))
    if project_id not in {p.get("id") for p in available_projects or []}:
        project_id = None

    time_scope = _clean(parsed.get("time_scope"))
    time_scope = time_scope.lower() if time_scope and time_scope.lower() in ("latest", "earliest") else None

    entities = parsed.get("catalog_entities")
    entities = [e for e in entities if e in CATALOG_ENTITIES] if isinstance(entities, list) else []

    raw_depth = str(parsed.get("response_depth") or "").lower().strip()
    response_depth = "explanatory" if raw_depth == "explanatory" else "concise"

    analysis = QueryAnalysis(
        intent=intent,
        response_depth=response_depth,
        speaker=speaker,
        meeting_id=meeting_id,
        project_id=project_id,
        cross_meeting=cross_meeting,
        time_scope=time_scope,
        date_from=_clean(parsed.get("date_from")),
        date_to=_clean(parsed.get("date_to")),
        catalog_entities=entities,
        needs_retrieval=parsed.get("needs_retrieval") is not False,
    )
    logger.info(f"[LANGGRAPH: ANALYZE_QUERY] '{query}' -> {analysis}")
    return analysis
