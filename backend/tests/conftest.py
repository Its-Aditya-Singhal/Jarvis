import os
import sys
from pathlib import Path

import pytest

# the face-and-liveness suites test the face sign-in, which ships switched off (voice-only);
# tests of the voice-only setup pass face_auth=False themselves
os.environ.setdefault("JARVIS_FACE_AUTH", "1")
# ... and the local-model suites test the Ollama brain; Gemini tests build their brain explicitly
os.environ.setdefault("JARVIS_LLM_PROVIDER", "ollama")
os.environ.setdefault("JARVIS_EMBED_MODEL", "bge-m3")

from jarvis.config import REPO_ROOT, Settings

MODELS = REPO_ROOT / "models"
SAMPLES = Path(__import__("insightface").__file__).parent / "data" / "images"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", models_dir=MODELS, api_token="test-token")


@pytest.fixture(scope="session")
def face_engine():
    from jarvis.auth.face.engine import FaceEngine

    engine = FaceEngine(MODELS)
    if not engine.load():
        pytest.skip("face models not downloaded")
    return engine


def pytest_collection_modifyitems(config, items):
    """Mac-only tests (hardware, `say`, AppleScript, the Apple GPU) are skipped
    on other systems, not deleted. JARVIS_RUN_MAC_TESTS=1 forces them to run."""
    if sys.platform == "darwin" or os.environ.get("JARVIS_RUN_MAC_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="Mac-only: needs macOS or Mac hardware")
    for item in items:
        if "mac" in item.keywords:
            item.add_marker(skip)
