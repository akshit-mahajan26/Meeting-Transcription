import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENGINE_DIR = ROOT_DIR / "FS-AI-Engine"
for p in (str(ROOT_DIR), str(ENGINE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from engine.services import *
