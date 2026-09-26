"""Download the local models JARVIS needs (one-time, then fully offline).

    backend/.venv/bin/python scripts/download_models.py
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"


def face() -> None:
    from insightface.utils import ensure_available

    path = ensure_available("models", "buffalo_l", root=str(MODELS))
    (Path(path).parent / "buffalo_l.zip").unlink(missing_ok=True)
    print(f"face models ready: {path}")


if __name__ == "__main__":
    face()
