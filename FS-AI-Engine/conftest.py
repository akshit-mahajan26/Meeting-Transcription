import sys
from pathlib import Path

# Add FS-AI-Engine and workspace root to sys.path so engine and app are always importable
AI_ENGINE_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = AI_ENGINE_DIR.parent
BACKEND_DIR = WORKSPACE_DIR / "FS-backend"

for p in (AI_ENGINE_DIR, WORKSPACE_DIR, BACKEND_DIR):
    p_str = str(p)
    if p_str not in sys.path:
        sys.path.insert(0, p_str)
