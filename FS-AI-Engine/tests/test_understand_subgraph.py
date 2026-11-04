# ruff: noqa: F811  (pytest fixtures are used as parameters named like their import)
"""Tests for the subgraph layout of the chat graph: understand -> (smalltalk | fallback | records | rag).

DB-backed tests reuse the `world` fixture of test_account_isolation (needs TEST_DATABASE_URL).
"""

import asyncio
import time
import uuid

import pytest
from engine.graphs import chat_nodes, chat_retrieval
from engine.graphs.chat_analysis import QueryAnalysis
from engine.graphs.chat_graph import compiled_chat_graph, route_after_filters, run_chat_graph
from engine.graphs.chat_subgraphs import build_understand_subgraph, route_after_retrieval
from test_account_isolation import _ask, _stub_rewrite, needs_db, world  # noqa: F401  (pytest fixture)


class CountingEmbeddings:
    """Counts embedding requests (the embed step swallows errors, so raising would not prove anything)."""
    model = "count"
    calls = 0

    async def generate_embeddings(self, queries):
        CountingEmbeddings.calls += 1
        return [[0.0] * 1536 for _ in queries]


@pytest.fixture
def no_embeddings(monkeypatch):
    CountingEmbeddings.calls = 0
    monkeypatch.setattr(chat_retrieval, "EmbeddingService", CountingEmbeddings)
    return CountingEmbeddings


def _stub_analysis(monkeypatch, analysis):
    async def rewrite(query, history):
        return query

    async def analyze(query, **_kwargs):
        return analysis

    monkeypatch.setattr(chat_nodes, "rewrite_query", rewrite)
    monkeypatch.setattr(chat_nodes, "analyze_query", analyze)


# ------------------------------- structure and routing -------------------------------

def test_compiled_graph_nests_the_three_subgraphs():
    nodes = set(compiled_chat_graph.get_graph(xray=1).nodes)
    for expected in (
        "understand:load_context", "understand:rewrite_query", "understand:analyze_query",
        "understand:resolve_scope", "understand:lookup_records",
        "records:llm_synthesis", "records:format_citations",
        "rag:embed_query", "rag:vector_retrieval", "rag:llm_synthesis", "rag:fallback", "rag:format_citations",
        "smalltalk", "fallback",
    ):
        assert expected in nodes, expected


@pytest.mark.parametrize("state,expected", [
    ({"query_intent": "smalltalk"}, "smalltalk"),
    ({"query_intent": "smalltalk", "no_meeting_on_date": "2024-01-01"}, "smalltalk"),  # smalltalk wins
    ({"no_meeting_on_date": "2024-01-01"}, "fallback"),
    ({"no_meeting_on_date": "2024-01-01", "skip_retrieval": True}, "fallback"),  # no meeting that day beats records
    ({"skip_retrieval": True}, "records"),
    ({"skip_retrieval": False}, "rag"),
    ({"query_intent": "catalog", "skip_retrieval": False}, "rag"),  # catalog that needs transcript text still retrieves
    ({}, "rag"),
])
def test_route_after_filters_table(state, expected):
    assert route_after_filters(state) == expected


@pytest.mark.asyncio
async def test_first_stage_runs_in_parallel_and_analysis_waits_for_both(monkeypatch):
    events = []

    async def slow_context(state):
        events.append("context_start")
        await asyncio.sleep(0.3)
        events.append("context_end")
        return {"available_meetings": [{"id": "m1", "title": "T", "date": None}], "available_projects": [], "available_speakers": ["Sam"]}

    async def slow_rewrite(query, history):
        events.append("rewrite_start")
        await asyncio.sleep(0.3)
        events.append("rewrite_end")
        return "rewritten"

    seen = {}

    async def analyze(query, **kwargs):
        seen["query"] = query
        seen["meetings"] = kwargs["available_meetings"]
        seen["speakers"] = kwargs["available_speakers"]
        events.append("analyze")
        return QueryAnalysis()

    monkeypatch.setattr(chat_nodes, "AsyncSessionLocal", None)
    monkeypatch.setattr("engine.graphs.chat_subgraphs.load_context_node", slow_context)
    monkeypatch.setattr(chat_nodes, "rewrite_query", slow_rewrite)
    monkeypatch.setattr(chat_nodes, "analyze_query", analyze)

    started = time.perf_counter()
    result = await build_understand_subgraph().ainvoke({"query": "q", "user_id": "u", "history": []})
    elapsed = time.perf_counter() - started

    assert elapsed < 0.55, f"first stage ran sequentially ({elapsed:.2f}s)"
    assert events.index("analyze") > max(events.index("context_end"), events.index("rewrite_end"))
    assert events[:2] == ["context_start", "rewrite_start"] or events[:2] == ["rewrite_start", "context_start"]
    assert seen["query"] == "rewritten"  # analysis sees the rewritten question
    assert seen["meetings"][0]["id"] == "m1" and seen["speakers"] == ["Sam"]  # ...and the loaded context
    assert result["query"] == "rewritten"


@pytest.mark.asyncio
async def test_no_database_keeps_caller_supplied_lists_and_never_opens_a_session(monkeypatch):
    _stub_analysis(monkeypatch, QueryAnalysis(meeting_id="m2", speaker="Sam"))
    monkeypatch.setattr(chat_nodes, "AsyncSessionLocal", None)
    meetings = [{"id": "m1", "title": "A", "date": "2026-01-01"}, {"id": "m2", "title": "B", "date": "2026-01-02"}]
    state = {"query": "q", "user_id": "u", "history": [], "available_meetings": meetings, "available_speakers": ["Sam"]}
    result = await build_understand_subgraph().ainvoke(state)
    assert result["meeting_id"] == "m2" and result["extracted_speakers"] == ["Sam"]
    assert result["available_meetings"] == meetings and result["meeting_scope_inferred"] is True


@pytest.mark.asyncio
async def test_database_failure_in_load_context_propagates_instead_of_hanging(monkeypatch):
    class BrokenSession:
        async def __aenter__(self):
            raise ConnectionError("db down")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(chat_nodes, "AsyncSessionLocal", lambda: BrokenSession())
    _stub_analysis(monkeypatch, QueryAnalysis())
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(build_understand_subgraph().ainvoke({"query": "q", "user_id": "u", "history": []}), 5)


@pytest.mark.asyncio
async def test_missing_user_id_is_rejected(monkeypatch):
    monkeypatch.setattr(chat_nodes, "AsyncSessionLocal", None)
    _stub_analysis(monkeypatch, QueryAnalysis())
    with pytest.raises(KeyError):
        await build_understand_subgraph().ainvoke({"query": "q", "history": []})


# ------------------------------- end to end on the real database -------------------------------

@needs_db
@pytest.mark.asyncio
async def test_smalltalk_route_answers_directly_without_retrieval_or_citations(world, monkeypatch, no_embeddings):
    accounts, llm = world
    _stub_analysis(monkeypatch, QueryAnalysis(intent="smalltalk", needs_retrieval=False, reply="Hello there!"))
    state, prompt = await _ask(llm, accounts["a"], "hi")
    assert state["answer"] == "Hello there!"
    assert state["routing_mode"] == "LANGGRAPH_SMALLTALK"
    assert state["citations"] == [] and state["citations_count"] == 0
    assert prompt == "" and no_embeddings.calls == 0  # no synthesis call, no embedding


@needs_db
@pytest.mark.asyncio
async def test_records_route_for_catalog_never_embeds_and_lists_only_own_data(world, monkeypatch, no_embeddings):
    accounts, llm = world
    a, b = accounts["a"], accounts["b"]
    _stub_analysis(monkeypatch, QueryAnalysis(
        intent="catalog", catalog_entities=["projects", "meetings", "participants"], needs_retrieval=False))
    state, prompt = await _ask(llm, a, "list everything")
    assert state["skip_retrieval"] is True and state["routing_mode"] == "LANGGRAPH_CATALOG"
    assert no_embeddings.calls == 0
    assert a["project"] in prompt and a["title"] in prompt and a["speaker"] in prompt
    for leaked in (b["project"], b["title"], b["meeting"]):
        assert leaked not in prompt


@needs_db
@pytest.mark.asyncio
async def test_records_route_for_attendance_never_embeds_and_names_the_meeting(world, monkeypatch, no_embeddings):
    accounts, llm = world
    a, b = accounts["a"], accounts["b"]
    _stub_analysis(monkeypatch, QueryAnalysis(intent="participant_involvement", speaker="Neha", needs_retrieval=False))
    state, prompt = await _ask(llm, a, "which meetings was Neha in?")
    assert state["skip_retrieval"] is True
    assert [m["id"] for m in state["relevant_meetings"]] == [a["meeting"]]
    assert no_embeddings.calls == 0
    assert a["title"] in prompt and b["title"] not in prompt


@needs_db
@pytest.mark.asyncio
async def test_catalog_that_needs_transcript_text_goes_through_rag(world, monkeypatch, no_embeddings):
    accounts, llm = world
    _stub_analysis(monkeypatch, QueryAnalysis(intent="catalog", catalog_entities=["meetings"], needs_retrieval=True))
    state, _ = await _ask(llm, accounts["a"], "list the meetings and what was said")
    assert state["skip_retrieval"] is False and no_embeddings.calls == 1


@needs_db
@pytest.mark.asyncio
async def test_rag_route_falls_back_when_the_account_has_no_transcripts(world, monkeypatch):
    _, llm = world
    _stub_rewrite(monkeypatch, None)
    nobody = {"uid": str(uuid.uuid4())}
    state, prompt = await _ask(llm, nobody, "what was said about the budget plan?")
    assert state["routing_mode"] == "LANGGRAPH_FALLBACK" and state["citations"] == []
    assert state["provenance_verified"] is False
    assert prompt == ""  # nothing to synthesise from


@needs_db
@pytest.mark.asyncio
async def test_rag_route_cites_the_askers_turns_and_keeps_history(world, monkeypatch):
    accounts, llm = world
    a = accounts["a"]
    _stub_rewrite(monkeypatch, None)
    history = [{"role": "user", "content": "earlier question marker-xyz"}, {"role": "assistant", "content": "earlier answer"}]
    llm.prompts.clear()
    state = await run_chat_graph(query="what was said about the budget plan?", user_id=a["uid"], history=history)
    prompt = "\n".join(llm.prompts)
    assert state["citations_count"] > 0 and {c["meeting_id"] for c in state["citations"]} == {a["meeting"]}
    assert state["history"] == history and "earlier question marker-xyz" in prompt
    assert state["routing_mode"] == "LANGGRAPH_RAG"


@needs_db
@pytest.mark.asyncio
async def test_concurrent_questions_from_two_accounts_do_not_mix(world, monkeypatch):
    accounts, _ = world
    a, b = accounts["a"], accounts["b"]
    _stub_rewrite(monkeypatch, None)

    async def run(user):
        return await run_chat_graph(query="what was said about the budget plan?", user_id=user["uid"])

    results = await asyncio.gather(*[run(u) for u in (a, b, a, b, a, b)])
    for user, state in zip((a, b, a, b, a, b), results, strict=True):
        assert {c["meeting_id"] for c in state["citations"]} == {user["meeting"]}
        assert all(user["marker"] in c["exact_raw_text"] for c in state["citations"] if c["exact_raw_text"])


@needs_db
@pytest.mark.asyncio
async def test_explicit_scope_survives_the_split_steps(world, monkeypatch):
    accounts, llm = world
    a = accounts["a"]
    _stub_analysis(monkeypatch, QueryAnalysis())
    state, _ = await _ask(llm, a, "what was said?", meeting_id=a["meeting"], project_id=a["project"])
    assert state["meeting_id"] == a["meeting"] and state["meeting_scope_inferred"] is False
    assert state["project_id"] == a["project"]


# ------------------------------- branches that need a misbehaving database -------------------------------

class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FlakySession:
    """Meetings and speakers load, but the projects query fails (and optionally the fallback project lookup too)."""
    fail_fallback = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def begin_nested(self):
        return _Nested()

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM projects" in sql:
            raise RuntimeError("projects table missing")
        if "SELECT DISTINCT project_id" in sql:
            if self.fail_fallback:
                raise RuntimeError("fallback failed too")
            return _Result([("p1",)])
        return _Result([])


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_fallback,expected", [(False, ["p1"]), (True, [])])
async def test_load_context_survives_a_missing_projects_table(monkeypatch, fail_fallback, expected):
    session = type("S", (_FlakySession,), {"fail_fallback": fail_fallback})
    monkeypatch.setattr(chat_nodes, "AsyncSessionLocal", lambda: session())
    result = await chat_nodes.load_context_node({"user_id": "u"})
    assert [p["id"] for p in result["available_projects"]] == expected
    assert result["available_meetings"] == [] and result["available_speakers"] == []


def test_route_after_retrieval_table():
    assert route_after_retrieval({"query_intent": "catalog"}) == "llm_synthesis"
    assert route_after_retrieval({"relevant_meetings": [{"id": "m"}]}) == "llm_synthesis"
    assert route_after_retrieval({"retrieved_chunks": [{"turn_id": "t"}]}) == "llm_synthesis"
    assert route_after_retrieval({"retrieved_chunks": []}) == "fallback"
    assert route_after_retrieval({}) == "fallback"


def test_chat_graph_still_exposes_its_former_public_names():
    from engine.graphs import chat_graph
    for name in ("build_chat_graph", "compiled_chat_graph", "route_after_filters",
                 "route_after_retrieval", "run_chat_graph", "smalltalk_node"):
        assert hasattr(chat_graph, name), name
