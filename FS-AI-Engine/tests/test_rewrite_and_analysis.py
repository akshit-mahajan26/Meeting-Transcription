"""Query rewriting and query analysis: structured routing from the LLM reply, and fallbacks when it fails."""

import json

import pytest
from engine.graphs import chat_analysis, chat_rewrite
from engine.graphs.chat_analysis import QueryAnalysis, analyze_query
from engine.graphs.chat_rewrite import rewrite_query
from test_temporal_meeting_resolution import AVAILABLE_MEETINGS


def _llm(reply, seen=None):
    class Llm:
        async def generate_chat_completion(self, messages, **_):
            if seen is not None:
                seen.append(messages)
            if isinstance(reply, Exception):
                raise reply
            return reply

    return lambda: Llm()


@pytest.mark.asyncio
async def test_rewrite_returns_the_standalone_question(monkeypatch):
    seen = []
    monkeypatch.setattr(chat_rewrite, "get_llm_client", _llm('"What was the most recent meeting Harsh was part of?"', seen))
    history = [
        {"role": "user", "content": "what meetings was harsh part of?"},
        {"role": "assistant", "content": "Wrong Title Inc. on 1 Jan 2020"},
    ]
    out = await rewrite_query("what was the most recent meeting he was part of", history)
    assert out == "What was the most recent meeting Harsh was part of?"
    sent = "\n".join(m["content"] for m in seen[0])
    assert "Wrong Title Inc." not in sent  # assistant answers never reach the rewrite
    assert "harsh" in sent


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [RuntimeError("down"), "", "x" * 2000])
async def test_rewrite_failure_or_unusable_reply_keeps_the_original(monkeypatch, reply):
    monkeypatch.setattr(chat_rewrite, "get_llm_client", _llm(reply))
    assert await rewrite_query("hello", []) == "hello"


@pytest.mark.asyncio
async def test_analysis_returns_structured_routing(monkeypatch):
    seen = []
    reply = json.dumps({
        "intent": "participant_involvement", "speaker": "harsh", "meeting_id": None, "cross_meeting": False,
        "project_id": "NOLOCODE", "time_scope": "latest", "date_from": None, "date_to": None,
        "catalog_entities": ["meetings"], "needs_retrieval": False, "reply": None,
    })
    monkeypatch.setattr(chat_analysis, "get_llm_client", _llm(reply, seen))
    res = await analyze_query(
        "What was the most recent meeting Harsh was part of?",
        available_speakers=["Harsh Vardhan Dixit", "Neha"],
        available_meetings=AVAILABLE_MEETINGS,
        available_projects=[{"id": "NOLOCODE", "name": "Project NOLOCODE"}],
    )
    assert res.intent == "participant_involvement"
    assert res.speaker == "Harsh Vardhan Dixit"  # mapped to the full name from the participant list
    assert res.time_scope == "latest"
    assert res.project_id == "NOLOCODE"
    assert res.catalog_entities == ["meetings"]
    assert res.needs_retrieval is False
    assert "Harsh Vardhan Dixit" in seen[0][-1]["content"] and "01KQBRC3M5TCY9WDC7SQVV3X6F" in seen[0][-1]["content"]


@pytest.mark.asyncio
async def test_analysis_drops_values_not_in_the_supplied_lists(monkeypatch):
    reply = json.dumps({
        "intent": "content_qa", "speaker": "Totally Invented", "meeting_id": "NOPE", "project_id": "GHOST",
        "time_scope": "sometime", "catalog_entities": ["meetings", "weather"],
    })
    monkeypatch.setattr(chat_analysis, "get_llm_client", _llm(reply))
    res = await analyze_query("q", available_speakers=["Neha"], available_meetings=AVAILABLE_MEETINGS, available_projects=[])
    assert (res.speaker, res.meeting_id, res.project_id, res.time_scope) == (None, None, None, None)
    assert res.catalog_entities == ["meetings"]
    assert res.needs_retrieval is True  # flag missing: retrieve


@pytest.mark.asyncio
async def test_analysis_meeting_title_is_mapped_to_its_id_and_cross_meeting_clears_it(monkeypatch):
    monkeypatch.setattr(chat_analysis, "get_llm_client", _llm('{"intent": "content_qa", "meeting_id": "Nolocode Meeting"}'))
    res = await analyze_query("q", available_meetings=AVAILABLE_MEETINGS)
    assert res.meeting_id == "01KQBRC3M5TCY9WDC7SQVV3X6F"
    monkeypatch.setattr(chat_analysis, "get_llm_client", _llm(
        '{"intent": "content_qa", "meeting_id": "01KQBRC3M5TCY9WDC7SQVV3X6F", "cross_meeting": true}'))
    res = await analyze_query("q", available_meetings=AVAILABLE_MEETINGS)
    assert res.cross_meeting is True and res.meeting_id is None


@pytest.mark.asyncio
async def test_analysis_carries_dates_and_smalltalk_reply(monkeypatch):
    monkeypatch.setattr(chat_analysis, "get_llm_client", _llm(
        '{"intent": "content_qa", "date_from": "2026-10-03", "date_to": null}'))
    res = await analyze_query("who attended meetings after oct 2", [])
    assert (res.date_from, res.date_to) == ("2026-10-03", None)

    monkeypatch.setattr(chat_analysis, "get_llm_client", _llm(
        '{"intent": "smalltalk", "speaker": "Neha", "needs_retrieval": true, "reply": "Hi! Ask me about your meetings."}'))
    res = await analyze_query("helo", available_speakers=["Neha"])
    assert res.intent == "smalltalk" and res.reply == "Hi! Ask me about your meetings."
    assert res.needs_retrieval is False and res.speaker is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [RuntimeError("down"), "not json"])
async def test_analysis_failure_falls_back_to_full_retrieval(monkeypatch, reply):
    monkeypatch.setattr(chat_analysis, "get_llm_client", _llm(reply))
    res = await analyze_query("hello", [])
    assert res == QueryAnalysis() and res.intent == "content_qa" and res.needs_retrieval is True
