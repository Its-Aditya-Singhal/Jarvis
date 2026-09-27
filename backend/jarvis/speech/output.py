"""Spoken output: a queue of phrases, synthesised sentence by sentence and
played on the default output device while the next sentence is prepared.

While the assistant speaks (and briefly after), ``muted()`` is true so the
microphone pipeline ignores its own voice instead of transcribing it.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

import numpy as np

from ..events import EventBus
from .text import split_sentences
from .tts import SAMPLE_RATE, TextToSpeech

log = logging.getLogger(__name__)

TAIL_MUTE_S = 0.4  # room echo after playback ends
LEVEL_PERIOD_S = 1 / 15
MAX_QUEUE = 4


class SoundPlayer:
    """Blocking playback through sounddevice, reporting the output level."""

    def play(self, audio: np.ndarray, sr: int, on_level: Callable[[float], None], stop: threading.Event) -> None:
        import sounddevice as sd

        chunk = sr // 20
        with sd.OutputStream(samplerate=sr, channels=1, dtype="float32") as stream:
            for i in range(0, len(audio), chunk):
                if stop.is_set():
                    break
                part = audio[i : i + chunk]
                rms = float(np.sqrt(np.mean(part**2))) if len(part) else 0.0
                on_level(float(np.clip(rms * 6, 0, 1)))
                stream.write(part.reshape(-1, 1))


class SpeechOutput:
    def __init__(
        self,
        tts: TextToSpeech,
        bus: EventBus,
        gender: Callable[[], str],
        player: SoundPlayer | None = None,
    ):
        self.tts = tts
        self.bus = bus
        self.gender = gender
        self.player = player or SoundPlayer()
        self._q: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self._stop_current = threading.Event()
        self._closed = threading.Event()
        self._speaking = False
        self._mute_until = 0.0
        self._last_level_t = 0.0
        self._thread: threading.Thread | None = None

    @property
    def speaking(self) -> bool:
        return self._speaking

    def muted(self) -> bool:
        return self._speaking or not self._q.empty() or time.monotonic() < self._mute_until

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="speech-out", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        self._stop_current.set()
        self._q.put(("", None))

    def say(self, text: str, gender: str | None = None) -> None:
        text = " ".join(text.split())
        if not text or not self.tts.ready:
            return
        while self._q.qsize() >= MAX_QUEUE:  # don't build up a backlog of stale speech
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._q.put((text, gender))

    def interrupt(self) -> None:
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._stop_current.set()

    def _level(self, level: float) -> None:
        now = time.monotonic()
        if now - self._last_level_t >= LEVEL_PERIOD_S:
            self._last_level_t = now
            self.bus.publish({"type": "level", "level": round(level, 3), "source": "assistant"})

    def _run(self) -> None:
        while not self._closed.is_set():
            text, gender = self._q.get()
            if self._closed.is_set():
                break
            self._stop_current.clear()
            self._speaking = True
            self.bus.publish({"type": "tts", "active": True, "text": text})
            try:
                for sentence in split_sentences(text):
                    if self._stop_current.is_set():
                        break
                    audio = self.tts.synth(sentence, gender or self.gender())
                    self.player.play(audio, SAMPLE_RATE, self._level, self._stop_current)
            except Exception:
                log.exception("speech output failed")
            finally:
                self._mute_until = time.monotonic() + TAIL_MUTE_S
                self._speaking = False
                self.bus.publish({"type": "tts", "active": False})
                self.bus.publish({"type": "level", "level": 0.0, "source": "assistant"})
