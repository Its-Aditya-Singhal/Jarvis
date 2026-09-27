"""Download the local models JARVIS needs (one-time, then fully offline) into models/.

    backend/.venv/bin/python scripts/download_models.py            # the required models
    backend/.venv/bin/python scripts/download_models.py medium     # + Whisper medium (Quality mode)

The packaged app does the same on first run, from the same list
(backend/jarvis/model_manifest.json): resumable, and every file is checked
against its SHA-256 before it is used.
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"
sys.path.insert(0, str(ROOT / "backend"))

from jarvis.downloads import ModelDownloader  # noqa: E402


def fetch(*packs: str) -> None:
    d = ModelDownloader(MODELS)
    wanted = [p for p in packs if any(x.id == p for x in d.packs)]  # e.g. no GPU build off Apple Silicon
    r = d.start(wanted)
    if not r["started"]:
        if r["reason"] != "already downloaded":
            raise SystemExit(r["reason"])
        print(f"{', '.join(wanted)}: already downloaded")
        return
    last = ""
    while d.running:
        st = d.status()
        line = f"{st['state']} {st['file'] or ''} {st['done_bytes'] / 1e6:.0f}/{st['total_bytes'] / 1e6:.0f} MB"
        if line != last:
            print(line, flush=True)
            last = line
        time.sleep(1)
    st = d.status()
    if st["state"] != "done":
        raise SystemExit(st["error"] or st["state"])
    print(f"{', '.join(wanted)}: ready in {MODELS}")


def face() -> None:
    fetch("face")


def liveness() -> None:
    fetch("liveness")


def voice() -> None:
    fetch("voice")


def speech(sizes: list[str]) -> None:
    for size in sizes:
        fetch(*(["stt", "stt_gpu"] if size == "small" else [f"stt_{size}", f"stt_{size}_gpu"]))
    fetch("tts")


if __name__ == "__main__":
    face()
    liveness()
    voice()
    speech(["small", *sys.argv[1:]])
