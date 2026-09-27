"""Download the local models JARVIS needs (one-time, then fully offline).

    backend/.venv/bin/python scripts/download_models.py
"""

import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"

ECAPA_URL = "https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb/resolve/main/"
ECAPA_FILES = ["embedding_model.ckpt", "hyperparams.yaml"]


def face() -> None:
    from insightface.utils import ensure_available

    path = ensure_available("models", "buffalo_l", root=str(MODELS))
    (Path(path).parent / "buffalo_l.zip").unlink(missing_ok=True)
    print(f"face models ready: {path}")


def voice() -> None:
    """ECAPA-TDNN speaker embedding weights (~83 MB). Silero VAD ships inside its pip package."""
    target = MODELS / "speechbrain" / "spkrec-ecapa-voxceleb"
    target.mkdir(parents=True, exist_ok=True)
    for name in ECAPA_FILES:
        dest = target / name
        if dest.exists():
            continue
        print(f"downloading {name} …")
        tmp = dest.with_suffix(".part")
        urllib.request.urlretrieve(ECAPA_URL + name, tmp)
        tmp.replace(dest)
    print(f"voice model ready: {target}")


if __name__ == "__main__":
    face()
    voice()
