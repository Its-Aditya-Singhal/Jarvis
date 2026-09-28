"""Speech-to-text with Whisper, fully offline.

Two engines, same model family:
  * MLX (Apple GPU) from ``models/whisper-mlx/<size>`` when ``mlx-whisper``
    is installed: several times faster, used automatically;
  * faster-whisper (CTranslate2, int8 on the CPU) from ``models/whisper/<size>``
    as the fallback.
 Language is restricted to
English or Hindi: if Whisper's own detection picks anything else (Hinglish is
sometimes taken for Urdu), the utterance is decoded again forcing the more
likely of the two. Segments decode lazily, so the common case is one pass. Hinglish usually comes out in Latin letters (English) or in
Devanagari (Hindi); ``text.to_latin`` makes either comparable.
"""

from __future__ import annotations

import logging
import threading
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..hardware import profile

log = logging.getLogger(__name__)

LANGS = ("en", "hi")
# Whisper's well-known inventions on near-silence
_HALLUCINATIONS = {"thank you.", "thanks for watching!", "thank you for watching.", "you", "."}


@dataclass
class Transcript:
    text: str
    language: str  # "en" | "hi"
    language_prob: float
    duration_s: float


class SpeechToText:
    def __init__(self, models_root: Path, size: str = "small", threads: int | None = None, engine: str = "auto"):
        self.size = size
        self.path = Path(models_root) / "whisper" / size
        self.mlx_path = Path(models_root) / "whisper-mlx" / size
        self.engine_pref = engine  # auto | mlx | cpu
        self.engine = "none"
        self.threads = threads or profile().threads
        self._model: Any = None  # faster-whisper model or _MlxWhisper
        self._lock = threading.Lock()
        self.error: str | None = None

    @property
    def ready(self) -> bool:
        return self._model is not None

    def _load_mlx(self) -> bool:
        has_weights = any((self.mlx_path / f).is_file() for f in ("weights.npz", "weights.safetensors"))
        if self.engine_pref == "cpu" or not has_weights:
            return False
        try:
            import mlx_whisper  # noqa: F401
            from mlx_whisper.load_models import load_model

            load_model(str(self.mlx_path))  # compile/warm once
            self._model = _MlxWhisper(str(self.mlx_path))
            self._model.transcribe(np.zeros(16000, np.float32), language="en", beam_size=1)
            self.engine = "mlx"
            self.error = None
            return True
        except Exception as exc:
            log.warning("MLX Whisper unavailable, using the CPU: %s", exc)
            self._model = None
            return False

    def load(self) -> bool:
        if self._model is not None:
            return True
        if self._load_mlx():
            return True
        if not (self.path / "model.bin").is_file():
            self.error = f"speech model missing at {self.path}"
            return False
        try:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                str(self.path), device="cpu", compute_type="int8", cpu_threads=self.threads
            )
            self.engine = "cpu"
            self.error = None
            return True
        except Exception as exc:
            log.exception("speech recognition failed to load")
            self.error = f"speech recognition failed to load: {exc}"
            return False

    def _decode(self, audio: np.ndarray, language: str | None, prompt: str | None):
        return self._model.transcribe(
            audio,
            language=language,
            beam_size=1 if self.engine == "mlx" else 3,
            initial_prompt=prompt,
            condition_on_previous_text=False,
            without_timestamps=True,
            vad_filter=False,  # utterances are already cut by Silero VAD
        )

    def transcribe(
        self, audio: np.ndarray, prompt: str | None = None, language: str | None = None
    ) -> Transcript:
        """``audio``: float32 mono at 16 kHz. Blocking; call from a worker thread."""
        if self._model is None:
            raise RuntimeError(self.error or "speech recognition not loaded")
        audio = np.asarray(audio, dtype=np.float32)
        with self._lock:
            segments, info = self._decode(audio, language, prompt)
            prob = info.language_probability if language is None else 1.0
            if info.language not in LANGS:
                p = {k: v for k, v in (info.all_language_probs or []) if k in LANGS}
                forced = max(LANGS, key=lambda k: p.get(k, 0.0))
                segments, info = self._decode(audio, forced, prompt)
                prob = p.get(forced, 0.0) / (sum(p.values()) or 1.0)
            language = info.language
            parts = [
                s.text.strip()
                for s in segments
                if s.no_speech_prob < 0.6 and s.avg_logprob > -1.2
            ]
        text = " ".join(p for p in parts if p).strip()
        if text.lower() in _HALLUCINATIONS:
            text = ""
        return Transcript(text, language, round(float(prob), 3), len(audio) / 16000)


class _Segment:
    def __init__(self, d: dict):
        self.text = d.get("text", "")
        self.no_speech_prob = float(d.get("no_speech_prob", 0.0))
        self.avg_logprob = float(d.get("avg_logprob", 0.0))


class _Info:
    def __init__(self, language: str | None):
        self.language = language
        self.language_probability = 1.0
        self.all_language_probs = [("en", 0.5), ("hi", 0.5)]


def degenerate(text: str) -> bool:
    """A decoding loop ("ौ ौ ौ …") or repeated phrase instead of real speech."""
    t = text.strip()
    if not t:
        return False
    words = t.split()
    if len(words) >= 6 and len(set(words)) <= max(2, len(words) // 4):
        return True
    raw = t.encode()
    return len(raw) > 40 and len(raw) / max(1, len(zlib.compress(raw))) > 2.4


class _MlxWhisper:
    """faster-whisper-shaped wrapper around mlx_whisper (greedy decoding on the GPU).

    The name prompt occasionally sends Whisper into a loop on Hindi audio; such
    output is detected and the clip decoded again without the prompt.
    """

    def __init__(self, path: str):
        self.path = path
        import mlx.core as mx

        # MLX keeps freed GPU buffers for reuse; for short clips that cache grows to
        # ~1 GB without making decoding any faster, so don't keep one
        mx.set_cache_limit(0)

    def _run(self, audio, language, prompt):
        import mlx_whisper

        return mlx_whisper.transcribe(
            np.asarray(audio, np.float32),
            path_or_hf_repo=self.path,
            language=language,
            initial_prompt=prompt,
            condition_on_previous_text=False,
            temperature=0.0,
            fp16=True,
            sample_len=96,  # commands are short; caps a runaway loop
            verbose=None,
        )

    def transcribe(self, audio, language=None, beam_size=1, initial_prompt=None, **_kw):
        out = self._run(audio, language, initial_prompt)
        if initial_prompt and degenerate(out.get("text", "")):
            out = self._run(audio, language, None)
        segs = [_Segment(d) for d in out.get("segments", [])]
        if degenerate(out.get("text", "")):
            segs = []
        return segs, _Info(out.get("language"))
