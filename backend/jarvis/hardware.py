"""What this Mac can afford: one profile, picked from its memory, sets the language model,
context size, how long models stay loaded, how many threads the on-device models use and
when idle models are unloaded.

An 8 GB Mac runs macOS, a browser and JARVIS in the same memory, so there JARVIS uses the
3B language model with a smaller context, releases models sooner and never loads Whisper
medium. ``JARVIS_RAM_GB`` overrides the detected size (for testing a small Mac on a big one).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Profile:
    name: str  # "small" | "standard"
    llm_model: str  # default language model (the owner can still choose another)
    num_ctx: int  # the language model's context: its memory grows with it; the command prompt alone is ~3K tokens
    num_parallel: int  # requests Ollama serves at once: each keeps its own context memory
    chat_keep_alive: str  # how long Ollama keeps the language model loaded after a request
    embed_keep_alive: str  # ... and the memory-recall model
    face_det_size: int  # face detector input: a laptop camera's face is large, 480 finds it
    threads: int  # CPU threads per on-device model (speech, voice, face)
    low_memory_pct: float  # system memory use at which the idle language model is unloaded
    idle_before_free_s: float  # ... once unused this long
    stt_medium: bool  # whether Quality mode may load Whisper medium (1.5 GB)
    max_face_fps: float  # face analysis rate cap (each frame costs CPU)


STANDARD = Profile("standard", "qwen2.5:7b", 6144, 2, "20m", "10m", 640, 8, 88.0, 120.0, True, 12.0)
SMALL = Profile("small", "qwen2.5:3b", 5120, 1, "5m", "2m", 480, 4, 80.0, 45.0, False, 4.0)

SMALL_MAX_GB = 12.0  # 8 GB Macs (and anything below 12 GB) get the small profile


def ram_gb() -> float:
    override = os.environ.get("JARVIS_RAM_GB")
    if override:
        try:
            gb = float(override)
            if gb > 0:
                return gb
        except ValueError:
            pass  # a typo must not stop the backend: use the real size
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2).stdout
        return int(out.strip()) / 2**30
    except (OSError, ValueError, subprocess.SubprocessError):
        try:
            import psutil

            return psutil.virtual_memory().total / 2**30
        except Exception:
            return 16.0


def profile_for(gb: float) -> Profile:
    return SMALL if gb < SMALL_MAX_GB else STANDARD


@lru_cache(maxsize=1)
def profile() -> Profile:
    return profile_for(ram_gb())
