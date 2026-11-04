import os
import sys

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(BACKEND_DIR), "FS-AI-Engine"))

from app.core.database import init_db  # noqa: E402
from app.main import app as backend_app  # noqa: E402

try:
    from engine.core.config import get_settings as engine_settings
    from engine.main import app as engine_app

    # The engine only answers calls that carry the shared internal key (as FS-backend sends)
    client_engine = TestClient(engine_app, headers={"X-Internal-Api-Key": engine_settings().engine_api_key})
except (ImportError, ModuleNotFoundError):
    client_engine = None

# Ensure database tables exist for test run
init_db()

client_backend = TestClient(backend_app)


@pytest.mark.skipif(client_engine is None, reason="FS-AI-Engine not importable in backend test env")
def test_ai_engine_health():
    res = client_engine.get("/health")
    assert res.status_code == 200
    assert res.json()["service"] == "FS-AI-Engine"


def test_backend_health():
    res = client_backend.get("/health")
    assert res.status_code == 200
    assert res.json()["service"] == "FS-backend"


@pytest.mark.skipif(client_engine is None, reason="FS-AI-Engine not importable in backend test env")
def test_ai_engine_normalize_endpoint():
    payload = {
        "meeting_id": "test_m1",
        "title": "Standalone AI Engine Test",
        "sentences": [
            {
                "index": 0,
                "speaker_name": "Carol",
                "text": "Part one.",
                "start_time": 1.0,
                "end_time": 2.0,
            },
            {
                "index": 1,
                "speaker_name": "Carol",
                "text": "Part two.",
                "start_time": 2.1,
                "end_time": 3.0,
            },
        ],
    }
    res = client_engine.post("/api/v1/transcript/normalize", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["total_normalized_turns"] == 1
    assert data["turns"][0]["turn_id"] == "mtg_test_m1_turn_001"
    assert data["turns"][0]["text"] == "Part one. Part two."


# ---------------------------------------------------------------------------
# Ingestion: the single entry point is POST /api/v1/meetings/ingest-link
# (used by the frontend Ingest page). These tests use fakes, never the live DB.
# ---------------------------------------------------------------------------
from app.api.deps import get_current_user  # noqa: E402
from app.api.v1.endpoints.ingest import parse_meeting_link  # noqa: E402
from app.core.database import UserModel  # noqa: E402
from app.services.fireflies_client import get_fireflies_client  # noqa: E402
from app.services.pipeline_orchestrator import get_backend_pipeline_orchestrator  # noqa: E402


class _FakeFireflies:
    async def fetch_transcript_by_id(self, transcript_id):
        return {"id": transcript_id, "title": "T", "sentences": []}

    async def upload_audio_from_url(self, audio_url, title=None):
        return {"uploaded": audio_url, "title": title}


class _FakeOrchestrator:
    def __init__(self):
        self.calls = []
        self.user_ids = []

    async def orchestrate_meeting_ingestion(self, transcript_dict, user_id, project_id=None, custom_title=None):
        self.calls.append((transcript_dict["id"], project_id, custom_title))
        self.user_ids.append(user_id)
        return {"status": "success"}


@pytest.fixture(autouse=True)
def authenticated_user():
    """Protected routes need a login; these tests act as a fixed account (auth itself is in test_auth_isolation)."""
    backend_app.dependency_overrides[get_current_user] = lambda: UserModel(id="u-test", email="u@test.com")
    yield
    backend_app.dependency_overrides.clear()


@pytest.fixture
def fake_ingest():
    fake_orch = _FakeOrchestrator()
    backend_app.dependency_overrides[get_fireflies_client] = lambda: _FakeFireflies()
    backend_app.dependency_overrides[get_backend_pipeline_orchestrator] = lambda: fake_orch
    yield fake_orch
    backend_app.dependency_overrides.clear()


def test_parse_meeting_link_variants():
    assert parse_meeting_link("https://app.fireflies.ai/view/Weekly-Sync::01ABC")["id"] == "01ABC"
    assert parse_meeting_link("https://app.fireflies.ai/view/Weekly-Sync::01ABC")["title"] == "Weekly Sync"
    assert parse_meeting_link("https://app.fireflies.ai/view/01XYZ")["id"] == "01XYZ"
    assert parse_meeting_link("https://cdn.example.com/rec.mp3")["type"] == "RAW_MEDIA"
    with pytest.raises(ValueError):
        parse_meeting_link("https://example.com/not-a-meeting")


def test_ingest_link_queues_fireflies_pipeline(fake_ingest):
    res = client_backend.post(
        "/api/v1/meetings/ingest-link",
        json={"link": "https://app.fireflies.ai/view/Weekly-Sync::01ABC", "project_id": "P1"},
    )
    assert res.status_code == 202
    assert res.json()["job_type"] == "FIREFLIES_DASHBOARD"
    # background task ran: transcript fetched, then handed to the orchestrator with project and derived title
    assert fake_ingest.calls == [("01ABC", "P1", "Weekly Sync")]
    assert fake_ingest.user_ids == ["u-test"]


def test_ingest_link_custom_title_wins(fake_ingest):
    client_backend.post(
        "/api/v1/meetings/ingest-link",
        json={"link": "https://app.fireflies.ai/view/Weekly-Sync::01ABC", "title": "My title"},
    )
    assert fake_ingest.calls == [("01ABC", None, "My title")]


def test_ingest_link_rejects_unsupported_link(fake_ingest):
    res = client_backend.post("/api/v1/meetings/ingest-link", json={"link": "https://example.com/page"})
    assert res.status_code == 400
    assert fake_ingest.calls == []


def test_meeting_read_endpoints_available():
    assert client_backend.get("/api/v1/meetings/").status_code == 200
    assert client_backend.get("/api/v1/meetings/projects").status_code == 200


@pytest.mark.parametrize("path", ["/api/v1/meetings/webhook", "/webhook", "/api/v1/meetings/ingest/raw", "/api/v1/meetings/ingest/fetch"])
def test_retired_ingestion_entry_points_are_gone(path):
    assert client_backend.post(path, json={}).status_code in (404, 405)
