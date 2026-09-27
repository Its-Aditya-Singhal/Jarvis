"""Speech-to-text with faster-whisper (CTranslate2, int8 on the CPU).

Runs fully offline from ``models/whisper/<size>``. Language is restricted to
English or Hindi: if Whisper's own detection picks anything else (Hinglish is
sometimes taken for Urdu), the utterance is decoded again forcing the more
likely of the two. Segments decode lazily, so the common case is one pass. Hinglish usually comes out in Latin letters (English) or in
Devanagari (Hindi); ``text.to_latin`` makes either comparable.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

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
    def __init__(self, models_root: Path, size: str = "small", threads: int = 8):
        self.size = size
        self.path = Path(models_root) / "whisper" / size
        self.threads = threads
        self._model = None
        self._lock = threading.Lock()
        self.error: str | None = None

    @property
    def ready(self) -> bool:
        return self._model is not None

    def load(self) -> bool:
        if self._model is not None:
            return True
        if not (self.path / "model.bin").is_file():
            self.error = f"speech model missing at {self.path}"
            return False
        try:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                str(self.path), device="cpu", compute_type="int8", cpu_threads=self.threads
            )
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
            beam_size=3,
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
