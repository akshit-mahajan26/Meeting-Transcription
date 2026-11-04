"""Engine-side account isolation: the shared-key gate, and every query scoped to one account.

Runs the real chat pipeline SQL against a throwaway Postgres migrated to head (embedding and LLM stubbed).
Set TEST_DATABASE_URL (asyncpg) to enable, e.g.
    TEST_DATABASE_URL="postgresql+asyncpg:///auth_migration_test?host=/tmp&port=5434"
"""

import os
import uuid

import pytest
import pytest_asyncio
from engine.api.routers import engine as engine_router
from engine.core.config import get_settings
from engine.graphs import chat_analysis, chat_nodes, chat_retrieval, chat_rewrite, chat_synthesis
from engine.graphs.chat_analysis import QueryAnalysis
from engine.graphs.chat_graph import run_chat_graph
from engine.main import app
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DB = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")
KEY = get_settings().engine_api_key
client = TestClient(app)


# ------------------------------- shared-key gate -------------------------------

def test_engine_rejects_calls_without_the_internal_key():
    body = {"query": "hi", "user_id": "u"}
    assert client.post("/api/v1/engine/query", json=body).status_code == 401
    assert client.post("/api/v1/engine/query", json=body, headers={"X-Internal-Api-Key": "wrong-key"}).status_code == 401
    assert client.post("/api/v1/transcript/embed", json={"texts": ["x"]}).status_code == 401
    assert client.get("/health").status_code == 200


def test_engine_query_requires_user_id_and_passes_it_through(monkeypatch):
    seen = {}

    async def fake_process(**kwargs):
        seen.update(kwargs)
        return {"query": kwargs["query"], "answer": "ok"}

    monkeypatch.setattr(engine_router, "process_user_query", fake_process)
    headers = {"X-Internal-Api-Key": KEY}
    assert client.post("/api/v1/engine/query", json={"query": "hi"}, headers=headers).status_code == 422
    assert client.post("/api/v1/engine/query", json={"query": "hi", "user_id": ""}, headers=headers).status_code == 422
    ok = client.post("/api/v1/engine/query", json={"query": "hi", "user_id": "u-1"}, headers=headers)
    assert ok.status_code == 200 and seen["user_id"] == "u-1"


# ------------------------------- data isolation -------------------------------

class StubEmbeddings:
    model = "stub"

    async def generate_embeddings(self, queries):
        return [[0.0] * 1536 for _ in queries]


class StubLLM:
    provider = "stub"

    def __init__(self):
        self.prompts: list[str] = []

    async def generate_chat_completion(self, messages, **kwargs):
        self.prompts.append("\n".join(m["content"] for m in messages))
        return "stub answer"


@pytest_asyncio.fixture
async def world(monkeypatch):
    """Two accounts, each with a project, a meeting and turns carrying a unique marker word."""
    db_engine = create_async_engine(TEST_DB)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    llm = StubLLM()
    for module in (chat_nodes, chat_retrieval):
        monkeypatch.setattr(module, "AsyncSessionLocal", factory)
    monkeypatch.setattr(chat_retrieval, "EmbeddingService", StubEmbeddings)
    monkeypatch.setattr(chat_synthesis, "get_llm_client", lambda: llm)
    monkeypatch.setattr(chat_rewrite, "get_llm_client", lambda: llm)
    monkeypatch.setattr(chat_analysis, "get_llm_client", lambda: llm)

    tag = uuid.uuid4().hex[:6]
    accounts = {}
    async with factory() as s:
        for name, speaker in (("a", "Neha"), ("b", "Priya")):
            uid = str(uuid.uuid4())
            acct = {"uid": uid, "project": f"proj-{name}-{tag}", "meeting": f"mtg-{name}-{tag}",
                    "title": f"Title {name.upper()} {tag}", "speaker": speaker, "marker": f"{name}marker{tag}"}
            accounts[name] = acct
            await s.execute(text("INSERT INTO users (id,email,password_hash,created_at) VALUES (:i,:e,'x',now())"),
                            {"i": uid, "e": f"{name}-{tag}@example.com"})
            await s.execute(text("INSERT INTO projects (id,user_id,name,status,created_at) VALUES (:p,:u,:p,'ACTIVE',now())"),
                            {"p": acct["project"], "u": uid})
            await s.execute(text("INSERT INTO meetings (id,user_id,project_id,title,date,status,created_at,updated_at) "
                                 "VALUES (:m,:u,:p,:t,'1780383600000','PROCESSED',now(),now())"),
                            {"m": acct["meeting"], "u": uid, "p": acct["project"], "t": acct["title"]})
            for n in (1, 2):
                await s.execute(text(
                    "INSERT INTO speaker_turns (turn_id,user_id,meeting_id,turn_number,speaker_id,speaker_name,text,"
                    "start_time,end_time,duration,sentence_indices,metadata) VALUES (:t,:u,:m,:n,'0',:sp,:tx,:ts,:ts,1,'[]',"
                    "CAST(:md AS jsonb))"),
                    {"t": f"mtg_{acct['meeting']}_turn_{n:03d}", "u": uid, "m": acct["meeting"], "n": n, "ts": float(n), "sp": speaker,
                     "tx": f"{speaker} discussed the {acct['marker']} budget plan", "md": f'{{"speaker": "{speaker}"}}'})
        # the other account also has someone called Neha, so name lookups could cross accounts
        b = accounts["b"]
        await s.execute(text(
            "INSERT INTO speaker_turns (turn_id,user_id,meeting_id,turn_number,speaker_id,speaker_name,text,"
            "start_time,end_time,duration,sentence_indices,metadata) VALUES (:t,:u,:m,3,'1','Neha',:tx,3,3,1,'[]',"
            "CAST('{\"speaker\": \"Neha\"}' AS jsonb))"),
            {"t": f"mtg_{b['meeting']}_turn_003", "u": b["uid"], "m": b["meeting"], "tx": f"Neha from B said {b['marker']}"})
        await s.commit()
    yield accounts, llm
    async with factory() as s:
        for acct in accounts.values():
            await s.execute(text("DELETE FROM users WHERE id=:u"), {"u": acct["uid"]})  # cascades to all their rows
        await s.commit()
    await db_engine.dispose()


def _stub_rewrite(monkeypatch, day):
    """The rewrite step normally reads the date from the question with an LLM; here it returns `day`."""
    async def rewrite(query, history):
        return query

    async def analyze(query, **_kwargs):
        return QueryAnalysis(date_from=day, date_to=day)

    monkeypatch.setattr(chat_nodes, "rewrite_query", rewrite)
    monkeypatch.setattr(chat_nodes, "analyze_query", analyze)


async def _ask(llm, user, query, **scope):
    llm.prompts.clear()
    state = await run_chat_graph(query=query, user_id=user["uid"], **scope)
    return state, "\n".join(llm.prompts)


@needs_db
@pytest.mark.asyncio
async def test_retrieval_only_returns_the_askers_own_turns(world):
    accounts, llm = world
    a, b = accounts["a"], accounts["b"]
    state_a, prompt_a = await _ask(llm, a, "what was said about the budget plan?")
    assert a["marker"] in prompt_a and b["marker"] not in prompt_a
    assert {c["meeting_id"] for c in state_a["citations"]} == {a["meeting"]}
    state_b, prompt_b = await _ask(llm, b, "what was said about the budget plan?")
    assert b["marker"] in prompt_b and a["marker"] not in prompt_b
    assert {c["meeting_id"] for c in state_b["citations"]} == {b["meeting"]}


@needs_db
@pytest.mark.asyncio
async def test_naming_someone_elses_meeting_project_or_speaker_returns_nothing(world):
    accounts, llm = world
    a, b = accounts["a"], accounts["b"]
    for scope in ({"meeting_id": b["meeting"]}, {"project_id": b["project"]}, {"speaker": b["speaker"]}):
        state, prompt = await _ask(llm, a, "what was said about the budget plan?", **scope)
        assert b["marker"] not in prompt, scope
        assert all(c["meeting_id"] != b["meeting"] for c in state["citations"]), scope
    # asking for a meeting by its title in plain words must not find it either
    _, prompt = await _ask(llm, a, f"summarize {b['title']}")
    assert b["marker"] not in prompt


@needs_db
@pytest.mark.asyncio
async def test_catalog_answers_list_only_the_askers_projects_meetings_and_people(world):
    accounts, llm = world
    a, b = accounts["a"], accounts["b"]
    for query in ("list all projects", "how many meetings are there?", "who are all the participants?"):
        _, prompt = await _ask(llm, a, query)
        for leaked in (b["project"], b["meeting"], b["title"], b["speaker"]):
            assert leaked not in prompt, (query, leaked)
    _, prompt = await _ask(llm, a, "list all projects")
    assert a["project"] in prompt
    _, prompt = await _ask(llm, a, "who are all the participants?")
    assert a["speaker"] in prompt


@needs_db
@pytest.mark.asyncio
async def test_which_meetings_did_someone_attend_ignores_same_named_people_in_other_accounts(world):
    accounts, llm = world
    a, b = accounts["a"], accounts["b"]
    state, prompt = await _ask(llm, a, "which meetings did Neha attend?")
    assert a["meeting"] in prompt or a["title"] in prompt
    for leaked in (b["meeting"], b["title"], b["marker"]):
        assert leaked not in prompt, leaked
    assert all(c["meeting_id"] != b["meeting"] for c in state["citations"])


@needs_db
@pytest.mark.asyncio
async def test_scoped_meeting_date_reaches_the_prompt(world):
    accounts, llm = world
    a = accounts["a"]
    _, prompt = await _ask(llm, a, "when did this meeting take place?", meeting_id=a["meeting"])
    assert "Held on: 2026-06-02 07:00 UTC" in prompt


@needs_db
@pytest.mark.asyncio
async def test_a_written_date_scopes_the_question_to_the_meeting_held_that_day(world, monkeypatch):
    accounts, llm = world
    _stub_rewrite(monkeypatch, "2026-06-02")
    a, b = accounts["a"], accounts["b"]
    state, prompt = await _ask(llm, a, "what was discussed in the meeting on 2026-06-02?")
    assert state["meeting_id"] == a["meeting"]
    assert a["marker"] in prompt and b["marker"] not in prompt


@needs_db
@pytest.mark.asyncio
async def test_a_date_with_no_meeting_answers_without_guessing_or_calling_the_llm(world, monkeypatch):
    accounts, llm = world
    _stub_rewrite(monkeypatch, "2024-01-01")
    state, prompt = await _ask(llm, accounts["a"], "summarize the meeting on 2024-01-01")
    assert "No meetings are recorded for 2024-01-01" in state["answer"]
    assert state["routing_mode"] == "LANGGRAPH_FALLBACK"
    assert prompt == ""  # the LLM was never called
