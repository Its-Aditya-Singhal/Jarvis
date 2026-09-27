"""Text-to-speech with Kokoro-82M (ONNX on the CPU) — offline.

The fp32 model is used when present: on Apple Silicon it synthesises about
three times faster than the int8 one (int8 matrix kernels are slow on ARM),
at 0.25x real time with 8 threads. The int8 model is the fallback.

The owner picks a female or male voice. English text uses American voices;
text containing Devanagari switches to the Hindi voice of the same gender
(phonemised by the bundled espeak-ng). Voices are stock Kokoro voices, not
imitations of any real person.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import numpy as np

from .text import has_devanagari

log = logging.getLogger(__name__)
# espeak warns on every Hindi sentence containing English words; that is expected
logging.getLogger("phonemizer").setLevel(logging.ERROR)

MODEL_FILES = ("kokoro-v1.0.onnx", "kokoro-v1.0.int8.onnx")  # preferred first
VOICES_FILE = "voices-v1.0.bin"
SAMPLE_RATE = 24000
GENDERS = ("female", "male")
VOICES = {
    ("female", "en"): "af_heart",
    ("male", "en"): "am_michael",
    ("female", "hi"): "hf_alpha",
    ("male", "hi"): "hm_omega",
}


class TextToSpeech:
    def __init__(self, models_root: Path, speed: float = 1.0, threads: int = 8):
        self.dir = Path(models_root) / "kokoro"
        self.speed = speed
        self.threads = threads
        self._k: Any = None  # kokoro_onnx.Kokoro
        self._lock = threading.Lock()
        self.error: str | None = None

    @property
    def ready(self) -> bool:
        return self._k is not None

    def load(self) -> bool:
        if self._k is not None:
            return True
        model = next((self.dir / f for f in MODEL_FILES if (self.dir / f).is_file()), self.dir / MODEL_FILES[-1])
        voices = self.dir / VOICES_FILE
        if not (model.is_file() and voices.is_file()):
            self.error = f"voice synthesis model missing at {self.dir}"
            return False
        try:
            import onnxruntime as ort
            from kokoro_onnx import Kokoro

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = self.threads
            session = ort.InferenceSession(str(model), opts, providers=["CPUExecutionProvider"])
            self._k = Kokoro.from_session(session, str(voices))
            self._k.create("Ready.", voice=VOICES[("female", "en")])  # warm-up
            self.error = None
            return True
        except Exception as exc:
            log.exception("speech synthesis failed to load")
            self.error = f"speech synthesis failed to load: {exc}"
            return False

    @staticmethod
    def voice_for(text: str, gender: str) -> tuple[str, str]:
        """(kokoro voice id, espeak language) for this text and gender."""
        g = gender if gender in GENDERS else "female"
        if has_devanagari(text):
            return VOICES[(g, "hi")], "hi"
        return VOICES[(g, "en")], "en-us"

    def synth(self, text: str, gender: str) -> np.ndarray:
        """Float32 mono audio at 24 kHz."""
        if self._k is None:
            raise RuntimeError(self.error or "speech synthesis not loaded")
        voice, lang = self.voice_for(text, gender)
        with self._lock:
            audio, _ = self._k.create(text, voice=voice, speed=self.speed, lang=lang)
        return np.asarray(audio, dtype=np.float32)
