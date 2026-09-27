"""Spoken output: a queue of phrases, synthesised sentence by sentence and
played on the default output device while the next sentence is prepared
(synthesis of sentence n+1 overlaps playback of sentence n). Short fixed
phrases are cached, and a listening "ping" replaces a spoken "Yes?".

While the assistant speaks (and briefly after), ``muted()`` is true so the
microphone pipeline ignores its own voice instead of transcribing it.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from ..events import EventBus
from .text import split_sentences
from .tts import SAMPLE_RATE, TextToSpeech

log = logging.getLogger(__name__)

TAIL_MUTE_S = 0.3  # room echo after playback ends
PING_MUTE_S = 0.1
CHIME = "\x00chime"
PING = "\x00ping"
CACHE_MAX_CHARS = 60  # short phrases ("Timer started.") are kept after first synthesis
CACHE_SIZE = 64


def _split_clause(sentence: str) -> tuple[str, str]:
    i = sentence.index(", ", 12)
    return sentence[:i], sentence[i + 2:]


def ping_audio(sr: int = SAMPLE_RATE) -> np.ndarray:
    """A short rising two-note blip: "I'm listening" (generated, no asset)."""
    def tone(f: float, dur: float) -> np.ndarray:
        t = np.arange(int(sr * dur)) / sr
        env = np.minimum(1, t / 0.005) * np.exp(-t * 18)
        return (0.25 * env * np.sin(2 * np.pi * f * t)).astype(np.float32)
    return np.concatenate([tone(988, 0.06), tone(1480, 0.1)])


def chime_audio(sr: int = SAMPLE_RATE) -> np.ndarray:
    """Two soft rising tones, twice — the alarm sound (generated, no asset)."""
    def tone(f: float, dur: float) -> np.ndarray:
        t = np.arange(int(sr * dur)) / sr
        env = np.minimum(1, t / 0.02) * np.exp(-t * 5)
        return (0.35 * env * (np.sin(2 * np.pi * f * t) + 0.3 * np.sin(4 * np.pi * f * t))).astype(np.float32)
    gap = np.zeros(int(sr * 0.08), np.float32)
    one = np.concatenate([tone(880, 0.32), gap, tone(1320, 0.45), gap * 3])
    return np.concatenate([one, one])
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
        self._cache: OrderedDict[tuple[str, str], np.ndarray] = OrderedDict()
        self._synth_pool = ThreadPoolExecutor(1, thread_name_prefix="tts")

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

    def chime(self) -> None:
        self._q.put((CHIME, None))

    def ping(self) -> None:
        self._q.put((PING, None))

    def synth(self, text: str, gender: str) -> np.ndarray:
        key = (text, gender)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        audio = self.tts.synth(text, gender)
        if len(text) <= CACHE_MAX_CHARS:
            self._cache[key] = audio
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return audio

    def clear_cache(self) -> None:
        self._cache.clear()

    def prewarm(self, phrases: list[str]) -> None:
        """Synthesise common phrases ahead of time (background, at startup)."""
        for p in phrases:
            for sentence in split_sentences(p):
                if self._closed.is_set():
                    return
                try:
                    self.synth(sentence, self.gender())
                except Exception:
                    log.debug("prewarm failed for %r", sentence)

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
            announced = False
            try:
                if text == CHIME:
                    self.player.play(chime_audio(), SAMPLE_RATE, self._level, self._stop_current)
                    continue
                if text == PING:
                    self.player.play(ping_audio(), SAMPLE_RATE, lambda _l: None, self._stop_current)
                    continue
                g = gender or self.gender()
                sentences = split_sentences(text)
                if sentences and len(sentences[0]) > 50 and ", " in sentences[0][12:]:
                    # start talking sooner: the first clause is synthesised on its own
                    head, tail = _split_clause(sentences[0])
                    sentences[0:1] = [head + ",", tail]
                nxt = self._synth_pool.submit(self.synth, sentences[0], g) if sentences else None
                for i in range(len(sentences)):
                    assert nxt is not None  # submitted for sentence i before the loop or in the last pass
                    audio = nxt.result()
                    # prepare the next sentence while this one plays
                    nxt = self._synth_pool.submit(self.synth, sentences[i + 1], g) if i + 1 < len(sentences) else None
                    if self._stop_current.is_set():
                        break
                    if not announced:  # when sound actually starts, not when synthesis does
                        announced = True
                        self.bus.publish({"type": "tts", "active": True, "text": text})
                    self.player.play(audio, SAMPLE_RATE, self._level, self._stop_current)
            except Exception:
                log.exception("speech output failed")
            finally:
                self._mute_until = time.monotonic() + (PING_MUTE_S if text == PING else TAIL_MUTE_S)
                self._speaking = False
                if announced:
                    self.bus.publish({"type": "tts", "active": False})
                self.bus.publish({"type": "level", "level": 0.0, "source": "assistant"})
