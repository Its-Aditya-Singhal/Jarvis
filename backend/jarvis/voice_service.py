"""Orchestrates microphone -> VAD -> speaker embedding -> enrollment / verification."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

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
from .security.crypto import is_keychain_error as _is_keychain_error
from .security.template_store import TemplateStore

log = logging.getLogger(__name__)

VOICE = "voice"
LEVEL_PERIOD_S = 1 / 15
UNKNOWN_VOICE_EVENT_GAP_S = 30.0
MIN_VERIFY_SPEECH_S = 0.8  # shorter clips carry too little speaker information: always "uncertain"


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
        self._life = threading.Lock()  # start() vs stop()
        self._thread: threading.Thread | None = None
        self._last_level_t = 0.0
        self._speaking = False
        self._last_unknown_event = 0.0
        self._buf = np.zeros(0, dtype=np.float32)
        # why the saved voice couldn't be opened at the last try ("keychain" / "unreadable"), else None
        self.profile_error: str | None = None

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
        out: dict[str, Any] = {"state": self.auth.state(now) if self.mode == "verifying" else "idle"}
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
        with self._life:
            if self._stop.is_set():  # the app closed while the models loaded
                return
            self.mic.start()
            self._thread = threading.Thread(target=self._loop, name="voice-loop", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._life:
            self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.mic.stop()

    # -- modes -------------------------------------------------------------------
    def begin_enrollment(self, needs_owner: bool) -> None:
        assistant, owner = self.names()
        session = VoiceEnrollmentSession(phrases(assistant, owner))
        with self._lock:
            self.enrollment = session
            self.enrollment_needs_owner = needs_owner
            self.mode = "enrolling"
        self.segmenter.reset()
        self.mic.drain()
        # the session itself: a cancel from another thread may already have cleared self.enrollment
        self.bus.publish({"type": "voice_enroll", **session.snapshot()})
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
        except Exception as exc:
            # the file stays on disk untouched: once macOS lets JARVIS read its key again it works
            self.profile_error = "keychain" if _is_keychain_error(exc) else "unreadable"
            log.exception("could not decrypt voice template")
            self.bus.log("Voice profile could not be decrypted", "error")
            return False
        self.profile_error = None
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
        vad = self.vad
        if vad is None:
            return
        self._buf = np.concatenate([self._buf, block])
        while len(self._buf) >= BLOCK:
            frame, self._buf = self._buf[:BLOCK], self._buf[BLOCK:]
            utt = self.segmenter.push(frame, vad(frame))
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
        if self.speech is not None and not session.relaxed and utt.speech_s >= MIN_SPEECH_S and q["score"] >= MIN_QUALITY:
            phrase = session.items[session.index]
            ok, heard = self.speech.check_phrase(utt.audio, phrase.text, phrase.lang)
            if not ok:
                session.miss("That didn't match the phrase — please read it exactly as shown")
                log.info("enrollment phrase mismatch: heard %r", heard)
                self.bus.publish({"type": "voice_enroll", **session.snapshot(), "accepted": False})
                return
        windows = self.engine.embed_windows(utt.audio)
        accepted = session.offer(windows, utt.speech_s, q["score"])
        self.bus.publish({"type": "voice_enroll", **session.snapshot(), "accepted": accepted})
        if accepted:
            self.bus.log(f"Voice sample {session.index}/{len(session.items)} captured")
        if session.done:
            with self._lock:  # cancel / delete / reset take this lock too
                if self.enrollment is not session:
                    return  # cancelled (or the profile deleted) while this phrase was processed
                template = session.template()
                self.store.save(VOICE, template)
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
            # too short to identify the speaker, but it may be "<name>", "louder" or "yes": "uncertain",
            # so only the harmless everyday commands run. It is never stitched to the clip before it:
            # the owner's "Jarvis" plus someone else's short "read my mail" could pass as the owner
            if self.speech is not None:
                self.speech.submit(utt.audio, self._too_short)
            return
        if self.speech is None:
            self._judge(utt, q, matcher)
            return
        # the speaker model runs only if the speech turns out to be addressed to the assistant:
        # talk in the room, calls and videos are never embedded (it was a second model per sentence)
        self.speech.submit(utt.audio, lambda: self._judge(utt, q, matcher))

    def _too_short(self) -> str:
        log.info("voice check uncertain: under %.1fs of speech", MIN_VERIFY_SPEECH_S)
        self.bus.log("Voice unclear — too short to identify the speaker", "warn")
        self.bus.publish({"type": "voice", "verdict": "uncertain"})
        return "uncertain"

    def _judge(self, utt: Utterance, q: dict, matcher: TemplateMatcher) -> str:
        return self._judge_audio(utt.audio, utt.speech_s, q, matcher)

    def _judge_audio(self, audio: np.ndarray, speech_s: float, q: dict, matcher: TemplateMatcher) -> str:
        sim = matcher.similarity(self.engine.embed(audio))
        now = time.monotonic()
        res = self.auth.judge(sim, q["score"], now, speech_s)
        # numbers only (no audio, no words): what a "couldn't confirm your voice" was made of, so the
        # thresholds can be checked against the owner's real scores
        log.info("voice check %s: similarity %.3f (accept %.2f, reject %.2f), quality %.2f, speech %.1fs",
                 res.verdict, sim, self.auth.threshold, self.auth.reject_threshold, q["score"], speech_s)
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
        elif q["score"] < self.auth.min_quality:
            self.bus.log("Voice unclear — too quiet or noisy to confirm the speaker", "warn")
        elif sim >= self.auth.reject_threshold:
            self.bus.log(f"Voice unclear — close to yours but not enough ({sim:.2f} of {self.auth.threshold:.2f})", "warn")
        else:
            self.bus.log("Voice unclear — too short to tell, and not much like yours", "warn")
        self.bus.publish({"type": "voice", "verdict": res.verdict})
        return res.verdict
