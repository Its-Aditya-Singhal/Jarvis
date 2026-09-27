"""Download the local models JARVIS needs (one-time, then fully offline).

    backend/.venv/bin/python scripts/download_models.py            # Whisper small
    backend/.venv/bin/python scripts/download_models.py medium     # + Whisper medium

On Apple Silicon with mlx-whisper installed, the GPU (MLX) build of the same
Whisper model is fetched too; speech recognition then runs on the GPU.
"""

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"

ECAPA_URL = "https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb/resolve/main/"
ECAPA_FILES = ["embedding_model.ckpt", "hyperparams.yaml"]
WHISPER_URL = "https://huggingface.co/Systran/faster-whisper-{size}/resolve/main/"
WHISPER_FILES = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]
WHISPER_MLX_URL = "https://huggingface.co/mlx-community/whisper-{size}-mlx/resolve/main/"
KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_FILES = ["kokoro-v1.0.onnx", "voices-v1.0.bin"]  # fp32: ~3x faster than int8 on Apple Silicon


def _fetch(base: str, names: list[str], target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name in names:
        dest = target / name
        if dest.exists():
            continue
        print(f"downloading {name} …")
        tmp = dest.with_suffix(dest.suffix + ".part")
        urllib.request.urlretrieve(base + name, tmp)
        tmp.replace(dest)


def face() -> None:
    from insightface.utils import ensure_available

    path = ensure_available("models", "buffalo_l", root=str(MODELS))
    (Path(path).parent / "buffalo_l.zip").unlink(missing_ok=True)
    print(f"face models ready: {path}")


def liveness() -> None:
    """Passive anti-spoof model (1.5 MB), SHA-256 verified by insightface's addon catalog."""
    from insightface.addons.catalog import ensure_addon

    print(f"liveness model ready: {ensure_addon('liveness', root=str(MODELS))}")


def voice() -> None:
    """ECAPA-TDNN speaker embedding weights (~83 MB). Silero VAD ships inside its pip package."""
    target = MODELS / "speechbrain" / "spkrec-ecapa-voxceleb"
    _fetch(ECAPA_URL, ECAPA_FILES, target)
    print(f"voice model ready: {target}")


def whisper_mlx(size: str) -> None:
    """The MLX conversion of Whisper (Apple GPU), used automatically when present."""
    try:
        import mlx_whisper  # noqa: F401
    except ImportError:
        print("mlx-whisper not installed: speech recognition will use the CPU build")
        return
    target = MODELS / "whisper-mlx" / size
    _fetch(WHISPER_MLX_URL.format(size=size), ["config.json", "weights.npz"], target)
    print(f"GPU speech recognition ready: {target}")


def speech(sizes: list[str]) -> None:
    """faster-whisper (CTranslate2) STT and Kokoro-82M TTS with its voice pack."""
    for size in sizes:
        target = MODELS / "whisper" / size
        _fetch(WHISPER_URL.format(size=size), WHISPER_FILES, target)
        print(f"speech recognition ready: {target}")
        whisper_mlx(size)
    _fetch(KOKORO_URL, KOKORO_FILES, MODELS / "kokoro")
    print(f"voice synthesis ready: {MODELS / 'kokoro'}")


if __name__ == "__main__":
    face()
    liveness()
    voice()
    speech(["small", *sys.argv[1:]])
