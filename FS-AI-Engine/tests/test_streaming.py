# ruff: noqa: F811  (pytest fixtures are used as parameters named like their import)
"""Streaming answers: the text filter, the LLM client stream, the synthesis node, the graph stream and the SSE endpoint."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from engine.core.config import get_settings
from engine.graphs import chat_nodes, chat_synthesis
from engine.graphs.chat_analysis import QueryAnalysis
from engine.graphs.chat_graph import run_chat_graph, stream_chat_graph
from engine.graphs.chat_subgraphs import build_records_subgraph
from engine.graphs.chat_synthesis import AnswerStreamFilter, split_sources_line
from engine.main import app
from engine.services.llm_client import LLMClient
from fastapi.testclient import TestClient
from test_account_isolation import _stub_rewrite, needs_db, world  # noqa: F401  (pytest fixture)

KEY = get_settings().engine_api_key
client = TestClient(app)


def _stream_text(text: str, size: int) -> str:
    f = AnswerStreamFilter()
    out = [f.feed(text[i:i + size]) for i in range(0, len(text), size)]
    out.append(f.flush())
    return "".join(out)


# ------------------------------- the text filter -------------------------------

SAMPLES = [
    "Plain answer with no sources.",
    "The team agreed on the budget.\nSOURCES: 1, 3",
    "The team agreed on the budget.\n\nSOURCES: 2",
    "Two paragraphs.\n\nSecond one here.\nSOURCES: none",
    "- First point\n- Second point\n**Sources:** 1, 2",
    "Sam said it [3] and later also [11, 13] was decided.\nSOURCES: 3, 11",
    "Sources of revenue grew this quarter.\nSOURCES: 1",
    "Source code review is on Friday.\nSOURCES: none",
    "Answer line\nsources: 4",
    "Answer\n> SOURCES: 1\n",
    "# Heading\nBody text.\nSOURCES: 2, 5\n",
    "No trailing newline then marker at the very end [7]",
    "Starts with S and So and Sou but is just words.\nSOURCES: none",
]


@pytest.mark.parametrize("text", SAMPLES)
@pytest.mark.parametrize("size", [1, 2, 3, 5, 8, 1000])
def test_filter_output_equals_the_final_cleaning_for_any_chunking(text, size):
    expected, _ = split_sources_line(text, 20)
    assert _stream_text(text, size).strip() == expected.strip()


def test_filter_holds_back_a_possible_sources_line_until_it_is_decided():
    f = AnswerStreamFilter()
    assert f.feed("Done.\nSou") == "Done.\n"      # "Sou" might still become "SOURCES:"
    assert f.feed("rces: 1, 2") == ""              # now it is, and it is never shown
    assert f.flush() == ""
    g = AnswerStreamFilter()
    assert g.feed("Done.\nSou") == "Done.\n"
    assert g.feed("nd bites") == "Sound bites"   # it was an ordinary word after all


def test_filter_holds_back_an_unfinished_excerpt_marker():
    f = AnswerStreamFilter()
    assert f.feed("It was decided [1") == "It was decided"  # the space before a marker goes with it
    assert f.feed(", 2] today") == " today"
    h = AnswerStreamFilter()
    assert h.feed("one ") == "one" and h.feed("two") == " two"  # a held space is released as soon as it is clear
    g = AnswerStreamFilter()
    assert g.feed("array [i") == "array [i"       # not a marker: released right away


def test_filter_streams_ordinary_text_without_delay():
    f = AnswerStreamFilter()
    assert f.feed("Hello wor") == "Hello wor"
    assert f.feed("ld") == "ld"


# ------------------------------- the LLM client stream -------------------------------

def _chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


class _FakeCompletions:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return await self.behaviour()


def _client_with(behaviour):
    llm = LLMClient.__new__(LLMClient)
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(behaviour)))
    llm._genai_client = None
    llm.default_model = "gemini-2.5-flash"
    llm.base_url = "http://stub"
    return llm


async def _collect(agen):
    return [x async for x in agen]


@pytest.mark.asyncio
async def test_client_streams_deltas_and_asks_for_a_stream():
    async def stream():
        async def gen():
            for t in ("Hel", "", "lo", " there"):
                yield _chunk(t)
            yield SimpleNamespace(choices=[])  # keep-alive chunk with no choices
        return gen()

    llm = _client_with(stream)
    out = await _collect(llm.stream_chat_completion([{"role": "user", "content": "hi"}], model="google/gemini-2.5-flash"))
    assert out == ["Hel", "lo", " there"]
    kwargs = llm._client.chat.completions.kwargs
    assert kwargs["stream"] is True and kwargs["model"] == "gemini-2.5-flash"  # provider prefix stripped like the non-stream call
    assert kwargs["max_tokens"] >= 8192


@pytest.mark.asyncio
async def test_client_skips_a_provider_that_fails_before_any_text():
    async def boom():
        raise ConnectionError("down")

    llm = _client_with(boom)
    out = await _collect(llm.stream_chat_completion([{"role": "user", "content": "hello"}]))
    assert len(out) == 1 and out[0]  # the offline fallback answered in one piece


@pytest.mark.asyncio
async def test_client_raises_when_a_stream_breaks_after_text_was_sent():
    async def stream():
        async def gen():
            yield _chunk("partial")
            raise ConnectionError("dropped")
        return gen()

    llm = _client_with(stream)
    seen = []
    with pytest.raises(ConnectionError):
        async for delta in llm.stream_chat_completion([{"role": "user", "content": "hi"}]):
            seen.append(delta)
    assert seen == ["partial"]  # never silently continued with a different provider


# ------------------------------- the synthesis node -------------------------------

class _StreamLlm:
    provider = "test"

    def __init__(self, deltas, fail_after=None, full="full answer\nSOURCES: 1"):
        self.deltas, self.fail_after, self.full = deltas, fail_after, full
        self.streamed = self.generated = 0

    async def stream_chat_completion(self, **_):
        self.streamed += 1
        for i, d in enumerate(self.deltas):
            if self.fail_after is not None and i == self.fail_after:
                raise ConnectionError("dropped")
            yield d

    async def generate_chat_completion(self, **_):
        self.generated += 1
        return self.full


CHUNK = {"text": "Budget approved.", "speaker": "Sam", "timestamp": "00:01", "meeting_id": "m1", "meeting_title": "T",
         "start_time": 1.0, "end_time": 2.0, "topic_context": "x", "similarity_score": 0.9}


async def _run_synthesis(monkeypatch, llm, stream):
    monkeypatch.setattr(chat_synthesis, "get_llm_client", lambda: llm)
    state = {"query": "q", "query_intent": "content_qa", "retrieved_chunks": [CHUNK], "history": []}
    graph = build_records_subgraph()
    tokens, final = [], None
    config = {"configurable": {"stream_answer": True}} if stream else None
    async for _ns, mode, payload in graph.astream(state, config=config, stream_mode=["custom", "values"], subgraphs=True):
        if mode == "custom":
            tokens.append(payload["text"])
        else:
            final = payload
    return tokens, final


@pytest.mark.asyncio
async def test_synthesis_streams_tokens_and_returns_the_same_result_as_without_streaming(monkeypatch):
    streamed = _StreamLlm(["Budget ", "approved", " [1].\nSOURCES: ", "1"], full="Budget approved.\nSOURCES: 1")
    tokens, final = await _run_synthesis(monkeypatch, streamed, stream=True)
    assert streamed.streamed == 1 and streamed.generated == 0
    assert "".join(tokens).strip() == "Budget approved."
    assert final["answer"] == "Budget approved." and final["used_sources"] == [1]
    assert "SOURCES" not in "".join(tokens) and "[1]" not in "".join(tokens)

    plain = _StreamLlm([], full="Budget approved [1].\nSOURCES: 1")
    tokens2, final2 = await _run_synthesis(monkeypatch, plain, stream=False)
    assert tokens2 == [] and plain.streamed == 0 and plain.generated == 1  # not asked to stream: nothing is streamed
    assert final2["answer"] == final["answer"] and final2["used_sources"] == final["used_sources"]


@pytest.mark.asyncio
async def test_synthesis_regenerates_in_one_piece_if_the_stream_breaks(monkeypatch):
    llm = _StreamLlm(["Bud", "get ", "appro", "ved"], fail_after=2, full="Budget approved.\nSOURCES: 1")
    _, final = await _run_synthesis(monkeypatch, llm, stream=True)
    assert llm.streamed == 1 and llm.generated == 1
    assert final["answer"] == "Budget approved."  # the canonical answer replaces the partial text that was shown


@pytest.mark.asyncio
async def test_synthesis_without_a_streaming_client_still_answers(monkeypatch):
    class Basic:
        provider = "x"

        async def generate_chat_completion(self, **_):
            return "ok\nSOURCES: none"

    tokens, final = await _run_synthesis(monkeypatch, Basic(), stream=True)
    assert tokens == [] and final["answer"] == "ok"


# ------------------------------- the graph stream (real database) -------------------------------

class _WorldStreamLlm:
    """Stub LLM for the DB tests: answers `stub answer` word by word and records prompts like StubLLM."""
    provider = "stub"

    def __init__(self):
        self.prompts = []

    async def stream_chat_completion(self, messages, **_):
        self.prompts.append("\n".join(m["content"] for m in messages))
        for piece in ("stub ", "answer", "\nSOURCES: 1"):
            yield piece

    async def generate_chat_completion(self, messages, **_):
        self.prompts.append("\n".join(m["content"] for m in messages))
        return "stub answer\nSOURCES: 1"


async def _events(user, query="what was said about the budget plan?", **scope):
    return [e async for e in stream_chat_graph(query=query, user_id=user["uid"], **scope)]


@pytest.fixture
def stream_llm(monkeypatch):
    llm = _WorldStreamLlm()
    monkeypatch.setattr(chat_synthesis, "get_llm_client", lambda: llm)
    return llm


@needs_db
@pytest.mark.asyncio
async def test_rag_question_streams_status_then_tokens_then_final_equal_to_the_normal_result(world, stream_llm, monkeypatch):
    accounts, _ = world
    a = accounts["a"]
    _stub_rewrite(monkeypatch, None)
    events = await _events(a)
    kinds = [k for k, _ in events]
    assert kinds[0] == "status" and events[0][1] == "Understanding your question…"
    assert ("status", "Searching transcripts…") in events
    assert kinds[-1] == "final" and kinds.count("final") == 1
    assert kinds.index("token") > kinds.index("status", 1)
    streamed = "".join(p for k, p in events if k == "token")
    final = events[-1][1]
    assert streamed.strip() == "stub answer" and final["answer"] == "stub answer"

    normal = await run_chat_graph(query="what was said about the budget plan?", user_id=a["uid"])
    for key in ("answer", "citations", "citations_count", "routing_mode", "meeting_id", "provenance_verified", "used_sources"):
        assert final[key] == normal[key], key
    assert final["latency_ms"] > 0


@needs_db
@pytest.mark.asyncio
async def test_smalltalk_streams_no_tokens_and_delivers_the_reply_in_the_final_event(world, stream_llm, monkeypatch):
    accounts, _ = world

    async def rewrite(q, h):
        return q

    async def analyze(q, **_):
        return QueryAnalysis(intent="smalltalk", needs_retrieval=False, reply="Hello there!")

    monkeypatch.setattr(chat_nodes, "rewrite_query", rewrite)
    monkeypatch.setattr(chat_nodes, "analyze_query", analyze)
    events = await _events(accounts["a"], "hi")
    assert [k for k, _ in events] == ["status", "final"]
    assert events[-1][1]["answer"] == "Hello there!" and events[-1][1]["routing_mode"] == "LANGGRAPH_SMALLTALK"
    assert stream_llm.prompts == []


@needs_db
@pytest.mark.asyncio
async def test_records_route_streams_with_its_own_status(world, stream_llm, monkeypatch):
    accounts, _ = world

    async def rewrite(q, h):
        return q

    async def analyze(q, **_):
        return QueryAnalysis(intent="catalog", catalog_entities=["projects"], needs_retrieval=False)

    monkeypatch.setattr(chat_nodes, "rewrite_query", rewrite)
    monkeypatch.setattr(chat_nodes, "analyze_query", analyze)
    events = await _events(accounts["a"], "list projects")
    assert ("status", "Looking up meeting records…") in events
    assert "".join(p for k, p in events if k == "token").strip() == "stub answer"
    assert events[-1][1]["routing_mode"] == "LANGGRAPH_CATALOG"


@needs_db
@pytest.mark.asyncio
async def test_nothing_found_streams_no_tokens_and_the_fallback_arrives_as_final(world, stream_llm, monkeypatch):
    _stub_rewrite(monkeypatch, None)
    events = await _events({"uid": str(uuid.uuid4())})
    assert "token" not in [k for k, _ in events]
    assert events[-1][1]["routing_mode"] == "LANGGRAPH_FALLBACK" and stream_llm.prompts == []


@needs_db
@pytest.mark.asyncio
async def test_streams_of_two_accounts_at_once_stay_separate(world, stream_llm, monkeypatch):
    accounts, _ = world
    a, b = accounts["a"], accounts["b"]
    _stub_rewrite(monkeypatch, None)
    results = await asyncio.gather(*[_events(u) for u in (a, b, a, b)])
    for user, events in zip((a, b, a, b), results, strict=True):
        final = events[-1][1]
        assert {c["meeting_id"] for c in final["citations"]} == {user["meeting"]}
    assert len(stream_llm.prompts) == 4
    for prompt in stream_llm.prompts:
        assert not (a["marker"] in prompt and b["marker"] in prompt), "an excerpt of one account reached the other's prompt"


@needs_db
@pytest.mark.asyncio
async def test_a_failure_in_the_pipeline_reaches_the_caller_after_the_progress_events(world, monkeypatch):
    accounts, _ = world
    _stub_rewrite(monkeypatch, None)

    class Broken:
        provider = "x"

        async def stream_chat_completion(self, **_):
            raise RuntimeError("llm down")
            yield  # pragma: no cover  (makes this an async generator)

        async def generate_chat_completion(self, **_):
            raise RuntimeError("llm down")

    monkeypatch.setattr(chat_synthesis, "get_llm_client", lambda: Broken())
    seen = []
    with pytest.raises(RuntimeError, match="llm down"):
        async for event in stream_chat_graph(query="what was said about the budget plan?", user_id=accounts["a"]["uid"]):
            seen.append(event)
    assert seen[0] == ("status", "Understanding your question…") and ("status", "Searching transcripts…") in seen
    assert "final" not in [k for k, _ in seen]

