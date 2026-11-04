"""Account isolation: signup/login, protected routes, and one account never seeing another's data.

Runs against a throwaway Postgres migrated to head. Set TEST_DATABASE_URL to enable, e.g.
    TEST_DATABASE_URL="postgresql+psycopg:///auth_migration_test?host=/tmp&port=5434"
"""

import os
import sys
import uuid

import pytest

TEST_DB = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(BACKEND_DIR), "FS-AI-Engine"))

from app.api.v1.endpoints import ingest as ingest_router  # noqa: E402
from app.api.v1.endpoints import query as query_router  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.database import MeetingModel, ProjectModel, SpeakerTurnModel, get_db  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

TestSession = sessionmaker(bind=create_engine(TEST_DB)) if TEST_DB else None


def _override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _use_test_db(monkeypatch):
    app.dependency_overrides[get_db] = _override_get_db
    # These tests create accounts, whatever ALLOW_SIGNUP the local .env sets
    monkeypatch.setattr(get_settings(), "allow_signup", True)
    yield
    app.dependency_overrides.clear()


client = TestClient(app)
PASSWORD = "secret1"


def _new_account() -> dict:
    email = f"user-{uuid.uuid4().hex[:8]}@example.com"
    res = client.post("/api/v1/auth/signup", json={"email": email, "password": PASSWORD})
    assert res.status_code == 201, res.text
    body = res.json()
    return {"email": email, "id": body["user"]["id"], "headers": {"Authorization": f"Bearer {body['access_token']}"}}


def _seed_for(user_id: str) -> dict:
    """One project, one meeting and one turn owned by the given account."""
    tag = uuid.uuid4().hex[:8]
    ids = {"project": f"proj-{tag}", "meeting": f"mtg-{tag}", "turn": f"mtg_{tag}_turn_001"}
    with TestSession() as db:
        db.add(ProjectModel(id=ids["project"], user_id=user_id, name="P"))
        db.flush()
        db.add(MeetingModel(id=ids["meeting"], user_id=user_id, project_id=ids["project"], title="Private", status="PROCESSED"))
        db.flush()
        db.add(SpeakerTurnModel(
            turn_id=ids["turn"], user_id=user_id, meeting_id=ids["meeting"], turn_number=1, speaker_id="0",
            speaker_name="Neha", text="secret words", start_time=0, end_time=1, duration=1,
        ))
        db.commit()
    return ids


# ------------------------------- auth -------------------------------

def test_signup_login_me_round_trip():
    acct = _new_account()
    login = client.post("/api/v1/auth/login", json={"email": acct["email"].upper(), "password": PASSWORD})
    assert login.status_code == 200
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
    assert me.status_code == 200 and me.json() == {"id": acct["id"], "email": acct["email"]}


def test_rejects_duplicate_wrong_password_and_bad_input():
    acct = _new_account()
    assert client.post("/api/v1/auth/signup", json={"email": acct["email"], "password": PASSWORD}).status_code == 409
    wrong = client.post("/api/v1/auth/login", json={"email": acct["email"], "password": "nope123"})
    unknown = client.post("/api/v1/auth/login", json={"email": "ghost@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()  # no hint whether the email exists
    assert client.post("/api/v1/auth/signup", json={"email": "not-an-email", "password": PASSWORD}).status_code == 422
    assert client.post("/api/v1/auth/signup", json={"email": "x@example.com", "password": "123"}).status_code == 422


def test_seeded_test_account_can_log_in():
    res = client.post("/api/v1/auth/login", json={"email": "test@test.com", "password": "123456"})
    assert res.status_code == 200, res.text


PUBLIC_PATHS = {"/api/v1/auth/signup", "/api/v1/auth/login", "/api/v1/health", "/health", "/"}


def test_every_protected_route_needs_a_valid_token():
    """Walks every registered API route, so a route added later without auth fails this test."""
    bad = {"Authorization": "Bearer not.a.token"}
    checked = 0
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api/v1") or path in PUBLIC_PATHS:
            continue
        url = path.replace("{meeting_id}", "x")
        for method in operations:
            for headers in ({}, bad):
                res = client.request(method.upper(), url, headers=headers, json={} if method == "post" else None)
                assert res.status_code == 401, (method, url, headers, res.status_code)
            checked += 1
    assert checked >= 7
    assert client.get("/health").status_code == 200


# ------------------------------- isolation -------------------------------

def test_accounts_only_see_their_own_data():
    a, b = _new_account(), _new_account()
    ids = _seed_for(a["id"])

    mine = client.get("/api/v1/meetings/", headers=a["headers"]).json()
    assert [m["id"] for m in mine["meetings"]] == [ids["meeting"]]
    assert client.get("/api/v1/meetings/projects", headers=a["headers"]).json()["projects"] == [ids["project"]]
    assert client.get(f"/api/v1/meetings/{ids['meeting']}", headers=a["headers"]).status_code == 200

    # the other account sees nothing and cannot reach it by ID
    assert client.get("/api/v1/meetings/", headers=b["headers"]).json()["meetings"] == []
    assert client.get("/api/v1/meetings/projects", headers=b["headers"]).json()["projects"] == []
    for path in (f"/api/v1/meetings/{ids['meeting']}", f"/api/v1/meetings/{ids['meeting']}/turns"):
        assert client.get(path, headers=b["headers"]).status_code == 404


def test_cannot_ingest_into_another_accounts_project_or_meeting():
    a, b = _new_account(), _new_account()
    ids = _seed_for(a["id"])
    calls = []

    class FakeOrchestrator:
        async def orchestrate_meeting_ingestion(self, **kw):
            calls.append(kw)

    class FakeFireflies:
        async def fetch_transcript_by_id(self, transcript_id):
            return {"id": transcript_id}

    app.dependency_overrides[ingest_router.get_backend_pipeline_orchestrator] = lambda: FakeOrchestrator()
    app.dependency_overrides[ingest_router.get_fireflies_client] = lambda: FakeFireflies()
    link = "https://app.fireflies.ai/view/Some-Title::{}"

    # B can use the same project ID as A without conflict (virtual namespacing)
    r1 = client.post("/api/v1/meetings/ingest-link", headers=b["headers"],
                     json={"link": link.format("NEWMEETING1"), "project_id": ids["project"]})
    assert r1.status_code == 202
    assert [c["user_id"] for c in calls] == [b["id"]]
    calls.clear()

    # B cannot ingest A's meeting ID (meeting collision)
    r2 = client.post("/api/v1/meetings/ingest-link", headers=b["headers"],
                     json={"link": link.format(ids["meeting"]), "project_id": "b-own-project"})
    assert r2.status_code == 409
    assert calls == []

    # A can ingest into their own project, and the task is stamped with A's id
    ok = client.post("/api/v1/meetings/ingest-link", headers=a["headers"],
                     json={"link": link.format("NEWMEETING2"), "project_id": ids["project"]})
    assert ok.status_code == 202
    assert [c["user_id"] for c in calls] == [a["id"]]


def test_query_proxy_forwards_verified_user_and_engine_key(monkeypatch):
    a = _new_account()
    seen = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"query": "q", "routing_mode": "M", "routing_confidence": 1.0, "routing_reasoning": "", "answer": "ok"}

    class FakeClient:
        def __init__(self, *args, **kwargs): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

        async def post(self, url, json=None, headers=None):
            seen.update(url=url, json=json, headers=headers)
            return FakeResponse()

    monkeypatch.setattr(query_router.httpx, "AsyncClient", FakeClient)
    res = client.post("/api/v1/query", headers=a["headers"], json={"query": "hi", "user_id": "someone-else"})
    assert res.status_code == 200
    assert seen["json"]["user_id"] == a["id"]  # from the token, not the request body
    assert seen["headers"]["X-Internal-Api-Key"] == query_router.settings.engine_api_key


# ------------------------------- full chain: backend -> engine -> database -------------------------------

def _wire_backend_to_engine(monkeypatch, engine_key=None):
    """Routes the backend's HTTP call to the real engine app in-process (real SQL on the test DB; LLM and embeddings stubbed)."""
    import httpx
    from engine.graphs import chat_nodes, chat_retrieval, chat_synthesis
    from engine.main import app as engine_app
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    # NullPool: TestClient runs each request on its own event loop, so connections must not be reused across them
    engine_db = create_async_engine(TEST_DB.replace("postgresql+psycopg", "postgresql+asyncpg"), poolclass=NullPool)
    factory = async_sessionmaker(engine_db, expire_on_commit=False)
    prompts = []

    class StubLLM:
        provider = "stub"

        async def generate_chat_completion(self, messages, **kwargs):
            prompts.append("\n".join(m["content"] for m in messages))
            return "stub answer"

    class StubEmbeddings:
        model = "stub"

        async def generate_embeddings(self, queries):
            return [[0.0] * 1536 for _ in queries]

    for module in (chat_nodes, chat_retrieval):
        monkeypatch.setattr(module, "AsyncSessionLocal", factory)
    monkeypatch.setattr(chat_retrieval, "EmbeddingService", StubEmbeddings)
    monkeypatch.setattr(chat_synthesis, "get_openrouter_client", lambda: StubLLM())

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.ASGITransport(app=engine_app)
        return real_client(*args, base_url="http://engine", **{k: v for k, v in kwargs.items() if k != "timeout"})

    monkeypatch.setattr(query_router.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(query_router.settings, "ai_engine_url", "http://engine")
    if engine_key:
        monkeypatch.setattr(query_router.settings, "engine_api_key", engine_key)
    return prompts


def test_query_flows_through_backend_and_engine_scoped_to_the_logged_in_account(monkeypatch):
    pytest.importorskip("engine.main")
    a, b = _new_account(), _new_account()
    ids_a, ids_b = _seed_for(a["id"]), _seed_for(b["id"])
    prompts = _wire_backend_to_engine(monkeypatch)

    res = client.post("/api/v1/query", headers=a["headers"], json={"query": "what did Neha say? secret words"})
    assert res.status_code == 200, res.text
    assert res.json()["answer"] == "stub answer"
    assert {c["meeting_id"] for c in res.json()["citations"]} == {ids_a["meeting"]}
    assert ids_b["meeting"] not in "\n".join(prompts)

    # naming B's meeting while logged in as A returns no excerpts from B
    res = client.post("/api/v1/query", headers=a["headers"], json={"query": "secret words", "meeting_id": ids_b["meeting"]})
    assert res.status_code == 200
    assert all(c["meeting_id"] != ids_b["meeting"] for c in res.json()["citations"])


def test_engine_key_mismatch_is_a_502_not_a_logout(monkeypatch):
    pytest.importorskip("engine.main")
    a = _new_account()
    _wire_backend_to_engine(monkeypatch, engine_key="a-different-key-that-is-long-enough")
    res = client.post("/api/v1/query", headers=a["headers"], json={"query": "hi"})
    assert res.status_code == 502


def test_background_ingestion_failure_is_recorded_and_visible(monkeypatch):
    from app.services import ingestion_failures as failures
    from app.services import pipeline_orchestrator as po
    from app.services.fireflies_client import FirefliesAPIError

    monkeypatch.setattr(failures, "SessionLocal", TestSession)
    a = _new_account()

    class BrokenFireflies:
        async def fetch_transcript_by_id(self, transcript_id):
            raise FirefliesAPIError("Fireflies returned HTTP 502: <internal body http://engine:8001>", status_code=502)

    meeting_id = f"fail-{uuid.uuid4().hex[:8]}"
    import asyncio
    asyncio.run(ingest_router._process_fireflies_dashboard_task(
        meeting_id, a["id"], None, "Weekly sync", BrokenFireflies(), po.BackendPipelineOrchestrator(),
    ))

    listed = client.get("/api/v1/meetings", headers=a["headers"]).json()
    failed = [m for m in (listed["meetings"] if isinstance(listed, dict) else listed) if m["id"] == meeting_id]
    assert failed and failed[0]["status"] == "FAILED"
    assert failed[0]["summary"] == "Could not reach Fireflies. Try again in a few minutes."
    assert "engine" not in failed[0]["summary"]  # raw exception text is not exposed
