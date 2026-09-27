"""Orchestrates microphone -> VAD -> speaker embedding -> enrollment / verification."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import numpy as np

from .audio.mic import BLOCK, Microphone
from .auth.matching import TemplateMatcher, confidence
from .auth.voice.engine import SpeakerEngine
from .auth.voice.enrollment import MIN_QUALITY, MIN_SPEECH_S, VoiceEnrollmentSession, phrases
from .auth.voice.quality import audio_quality
from .auth.voice.vad import Segmenter, SileroVAD, Utterance
from .auth.voice.verification import VoiceAuth
from .config import Settings
from .database.db import Database
from .events import EventBus
from .security.template_store import TemplateStore

log = logging.getLogger(__name__)

VOICE = "voice"
LEVEL_PERIOD_S = 1 / 15
UNKNOWN_VOICE_EVENT_GAP_S = 30.0
MIN_VERIFY_SPEECH_S = 0.8  # shorter clips carry too little speaker information


class VoiceService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        store: TemplateStore,
        bus: EventBus,
        engine: SpeakerEngine,
        mic: Microphone,
        owner_verified: Callable[[], bool],
        names: Callable[[], tuple[str, str]],
        vad_factory: Callable[[], Callable[[np.ndarray], float]] = SileroVAD,
        speech=None,  # SpeechService: transcription, wake word, muting while speaking
    ):
        self.s = settings
        self.db = db
        self.store = store
        self.bus = bus
        self.engine = engine
        self.mic = mic
        self.owner_verified = owner_verified
        self.names = names
        self._vad_factory = vad_factory
        self.speech = speech
        self.vad: Callable[[np.ndarray], float] | None = None
        self.segmenter = Segmenter()
        self.mode = "idle"  # idle | enrolling | verifying
        self.enrollment: VoiceEnrollmentSession | None = None
        self.enrollment_needs_owner = False
        self.matcher: TemplateMatcher | None = None
        self.auth = VoiceAuth(
            threshold=settings.voice_threshold,
            reject_threshold=settings.voice_reject_threshold,
            valid_s=settings.voice_valid_s,
        )
        self.error: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_level_t = 0.0
        self._speaking = False
        self._last_unknown_event = 0.0
        self._buf = np.zeros(0, dtype=np.float32)

    # -- status ----------------------------------------------------------------
    @property
    def enrolled(self) -> bool:
        return self.store.exists(VOICE)

    @property
    def ready(self) -> bool:
        return self.engine.ready and self.vad is not None

    @property
    def available(self) -> bool:
        """Voice can be used right now (models loaded and a working microphone)."""
        return self.ready and self.mic.status == "active"

    def model_status(self) -> str:
        if self.ready:
            return "ready"
        return self.error or self.engine.error or "loading"

    def public(self, owner_verified: bool) -> dict:
        now = time.monotonic()
        out = {"state": self.auth.state(now) if self.mode == "verifying" else "idle"}
        last = self.auth.last
        # similarity is only shown to the verified owner
        if owner_verified and last is not None and out["state"] != "idle":
            out["confidence"] = round(confidence(last.similarity, self.s.voice_threshold, 10.0), 3)
            out["seconds_ago"] = round(now - last.t, 1)
        return out

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        try:
            self.vad = self._vad_factory()
        except Exception as exc:
            log.exception("VAD failed to load")
            self.error = f"voice activity detector failed to load: {exc}"
        if self.engine.load():
            self.bus.log("Speaker recognition model loaded")
        else:
            self.bus.log(self.engine.error or "Voice model unavailable", "error")
        self.mic.start()
        self._thread = threading.Thread(target=self._loop, name="voice-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.mic.stop()

    # -- modes -------------------------------------------------------------------
    def begin_enrollment(self, needs_owner: bool) -> None:
        assistant, owner = self.names()
        with self._lock:
            self.enrollment = VoiceEnrollmentSession(phrases(assistant, owner))
            self.enrollment_needs_owner = needs_owner
            self.mode = "enrolling"
        self.segmenter.reset()
        self.mic.drain()
        self.bus.publish({"type": "voice_enroll", **self.enrollment.snapshot()})
        self.bus.log("Voice enrollment started")

    def cancel_enrollment(self, reason: str = "Voice enrollment cancelled") -> None:
        with self._lock:
            if self.enrollment is None:
                return
            self.enrollment = None
            self.mode = "verifying" if self.matcher else "idle"
        self.bus.publish({"type": "voice_enroll_cancelled", "reason": reason})
        self.bus.log(reason, "warn")

    def begin_verification(self) -> bool:
        try:
            template = self.store.load(VOICE)
        except Exception:
            log.exception("could not decrypt voice template")
            self.bus.log("Voice profile could not be decrypted", "error")
            return False
        if template is None:
            return False
        with self._lock:
            self.matcher = TemplateMatcher(template, top_k=self.s.voice_top_k)
            self.auth.reset()
            self.mode = "verifying"
        self.bus.log("Continuous voice verification active")
        return True

    # -- audio loop --------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            block = self.mic.read(timeout=0.5)
            now = time.monotonic()
            muted = self.speech is not None and self.speech.muted()
            if not muted and now - self._last_level_t >= LEVEL_PERIOD_S and self.bus.has_subscribers:
                self._last_level_t = now
                level = self.mic.level if self.mic.status == "active" else 0.0
                self.bus.publish({"type": "level", "level": round(level, 3)})
            if block is None or self.vad is None:
                continue
            try:
                self._process(block)
            except Exception:
                log.exception("voice loop failed")

    def _process(self, block: np.ndarray) -> None:
        if self.speech is not None and self.speech.muted():
            # the assistant is talking: don't segment (or transcribe) its own voice
            self.segmenter.reset()
            self._buf = np.zeros(0, dtype=np.float32)
            if self._speaking:
                self._speaking = False
                self.bus.publish({"type": "speaking", "active": False})
            return
        self._buf = np.concatenate([self._buf, block])
        while len(self._buf) >= BLOCK:
            frame, self._buf = self._buf[:BLOCK], self._buf[BLOCK:]
            utt = self.segmenter.push(frame, self.vad(frame))
            if self.segmenter.active != self._speaking:
                self._speaking = self.segmenter.active
                self.bus.publish({"type": "speaking", "active": self._speaking})
            if utt is not None:
                self._on_utterance(utt)

    def _on_utterance(self, utt: Utterance) -> None:
        if not self.engine.ready:
            return
        q = audio_quality(utt.audio, utt.speech_s, utt.noise_rms)
        if self.mode == "enrolling":
            self._enroll_utterance(utt, q)
        elif self.mode == "verifying":
            self._verify_utterance(utt, q)

    def _enroll_utterance(self, utt: Utterance, q: dict) -> None:
        with self._lock:
            session = self.enrollment
            needs_owner = self.enrollment_needs_owner
        if session is None:
            return
        if needs_owner and not self.owner_verified():
            # re-enrollment after setup requires the verified owner to stay at the screen
            self.cancel_enrollment("Voice enrollment stopped — owner no longer verified")
            return
        if self.speech is not None and utt.speech_s >= MIN_SPEECH_S and q["score"] >= MIN_QUALITY:
            phrase = session.items[session.index]
            ok, heard = self.speech.check_phrase(utt.audio, phrase.text, phrase.lang)
            if not ok:
                session.hint = "That didn't match the phrase — please read it exactly as shown"
                log.info("enrollment phrase mismatch: heard %r", heard)
                self.bus.publish({"type": "voice_enroll", **session.snapshot(), "accepted": False})
                return
        windows = self.engine.embed_windows(utt.audio)
        accepted = session.offer(windows, utt.speech_s, q["score"])
        self.bus.publish({"type": "voice_enroll", **session.snapshot(), "accepted": accepted})
        if accepted:
            self.bus.log(f"Voice sample {session.index}/{len(session.items)} captured")
        if session.done:
            template = session.template()
            self.store.save(VOICE, template)
            with self._lock:
                self.enrollment = None
            self.bus.log(f"Voice profile saved ({len(template)} encrypted samples)", "ok")
            self.bus.publish({"type": "voice_enroll_complete"})
            if self.matcher is not None or self.db.get("setup_complete") == "1":
                self.begin_verification()
            else:
                with self._lock:
                    self.mode = "idle"

    def _verify_utterance(self, utt: Utterance, q: dict) -> None:
        matcher = self.matcher
        if matcher is None:
            return
        if utt.speech_s < MIN_VERIFY_SPEECH_S:
            # too short to identify the speaker, but it may be "<name>" or "yes"
            if self.speech is not None:
                self.speech.submit(utt.audio, None)
            return
        sim = matcher.similarity(self.engine.embed(utt.audio))
        now = time.monotonic()
        res = self.auth.judge(sim, q["score"], now)
        if res.verdict == "verified":
            self.bus.log("Voice verified — owner identified", "ok")
        elif res.verdict == "rejected":
            self.bus.log("Voice not recognised", "alert")
            if now - self._last_unknown_event > UNKNOWN_VOICE_EVENT_GAP_S:
                self._last_unknown_event = now
                self.db.add_security_event(
                    "unknown_voice",
                    "Unrecognised voice near the device",
                    voice_conf=round(confidence(sim, self.s.voice_threshold, 10.0), 3),
                )
        else:
            self.bus.log("Voice unclear — could not confirm speaker", "warn")
        self.bus.publish({"type": "voice", "verdict": res.verdict})
        if self.speech is not None:
            self.speech.submit(utt.audio, res.verdict)
