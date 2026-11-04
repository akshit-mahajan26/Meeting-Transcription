import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import frontend_config
from services.api_client import MeetingIntelligenceClient


def test_frontend_config_defaults():
    """Verifies that frontend_config loads expected defaults."""
    assert frontend_config.FASTAPI_BACKEND_URL is not None
    assert frontend_config.QUERY_ENDPOINT == "/api/v1/query"
    assert frontend_config.REQUEST_TIMEOUT > 0


def test_api_client_query_backend_success():
    """Verifies query_backend parses answer and Level 1 citations."""
    client = MeetingIntelligenceClient(base_url="http://mock-backend:8000")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "query": "What did Sarah say?",
        "routing_mode": "MICRO",
        "routing_confidence": 0.95,
        "routing_reasoning": "Micro query",
        "answer": "Sarah mentioned verifying the normalization pipeline.",
        "citations": [
            {
                "speaker": "Sarah Connor",
                "start_time": 0.0,
                "end_time": 6.5,
                "meeting_id": "mtg_sample_101",
                "exact_raw_text": "We need to verify the normalization pipeline.",
                "topic_context": "Turn #1"
            }
        ],
        "citations_count": 1,
        "provenance_verified": True
    }

    with patch("httpx.Client.post", return_value=mock_resp):
        res = client.query_backend("What did Sarah say?")
        assert res["success"] is True
        assert res["routing_mode"] == "MICRO"
        assert len(res["citations"]) == 1
        assert res["citations"][0]["speaker"] == "Sarah Connor"


def test_frontend_app_syntax_and_imports():
    """Verifies FS-frontend/app.py can be compiled cleanly."""
    import os
    import py_compile
    base = "FS-frontend/" if os.path.exists("FS-frontend/app.py") else ""
    for f in ("app.py", "ui/styles.py", "ui/components.py", "ui/state.py", "ui/scope.py", "ui/shell.py", "ui/nav.py",
              "app_pages/overview.py", "app_pages/assistant.py", "app_pages/meetings.py",
              "app_pages/ingest.py", "utils/formatting.py"):
        py_compile.compile(base + f, doraise=True)
    cfg_path = "FS-frontend/config.py" if os.path.exists("FS-frontend/config.py") else "config.py"
    client_path = "FS-frontend/services/api_client.py" if os.path.exists("FS-frontend/services/api_client.py") else "services/api_client.py"
    py_compile.compile(cfg_path, doraise=True)
    py_compile.compile(client_path, doraise=True)


def test_formatting_helpers():
    from utils.formatting import esc, format_timestamp, parse_meeting_id, speaker_talk_time

    assert format_timestamp(75) == "01:15"
    assert format_timestamp(3725) == "1:02:05"
    assert esc("<b>x</b>") == "&lt;b&gt;x&lt;/b&gt;"
    assert parse_meeting_id("Weekly sync (mtg_1)") == "mtg_1"
    assert speaker_talk_time([{"speaker_name": "A", "start_time": 0, "end_time": 5}]) == {"A": 5.0}
