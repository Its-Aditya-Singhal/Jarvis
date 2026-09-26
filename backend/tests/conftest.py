from pathlib import Path

import pytest

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
