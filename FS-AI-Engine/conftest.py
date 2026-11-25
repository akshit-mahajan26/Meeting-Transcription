import sys
from pathlib import Path

import pytest

# Add FS-AI-Engine and workspace root to sys.path so engine and app are always importable
AI_ENGINE_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = AI_ENGINE_DIR.parent
BACKEND_DIR = WORKSPACE_DIR / "FS-backend"

for p in (AI_ENGINE_DIR, WORKSPACE_DIR, BACKEND_DIR):
    p_str = str(p)
    if p_str not in sys.path:
        sys.path.insert(0, p_str)


@pytest.fixture(autouse=True)
def _offline_providers(monkeypatch):
    """Tests never call the real Gemini APIs, whatever key the local .env holds (offline fallbacks answer instead)."""
    from engine.core.config import settings
    from engine.services import llm_client

    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)  # reload_config would re-read the key from .env
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(llm_client.llm_client_instance, "_client", None)
    monkeypatch.setattr(llm_client.llm_client_instance, "_genai_client", None)
