"""Orchestrates the face + liveness pipeline and composes it with the voice pipeline.

The session counts as the verified owner only when the face matches AND the
liveness gate has confirmed a live person (see ``effective_state``).
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from typing import Callable

import cv2
import numpy as np

from .auth.face.continuous import ContinuousFaceAuth, FrameResult
from .auth.face.engine import FaceEngine
from .auth.face.enrollment import EnrollmentSession
from .auth.face.types import FaceObservation
from .auth.liveness.gate import LiveObs, LivenessConfig, LivenessGate
from .auth.liveness.replay import FrozenFeedDetector
from .auth.matching import TemplateMatcher, confidence
from .camera.capture import Camera
from .config import Settings
from .database.db import Database
from .events import EventBus
from .security.template_store import TemplateStore
from .voice_service import VoiceService

log = logging.getLogger(__name__)

FACE = "face"


class AssistantService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        store: TemplateStore,
        bus: EventBus,
        engine: FaceEngine,
        camera: Camera,
        voice_factory: "Callable[[AssistantService], VoiceService] | None" = None,
    ):
        self.s = settings
        self.db = db
        self.store = store
        self.bus = bus
        self.engine = engine
        self.camera = camera
        self.mode = "idle"  # idle | enrolling | verifying
        self.enrollment: EnrollmentSession | None = None
        self.verifier: TemplateMatcher | None = None
        self.auth = self._new_auth()
        self.live = self._new_gate()
        self.frozen = FrozenFeedDetector()
        self._frozen = False
        self._unlocked = False  # greeted in this presence session
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_state_push = 0.0
        self._lock = threading.Lock()
        self.voice: VoiceService | None = voice_factory(self) if voice_factory else None

    def owner_verified(self) -> bool:
        return self.effective_state() == "approved"

    def effective_state(self) -> str:
        """Public auth state: face match combined with liveness.

        no_profile | scanning | liveness | approved | denied | absent | spoof
        """
        if self.mode != "verifying":
            return "no_profile"
        face = self.auth.state
        if not self.s.liveness_enabled:
            return face
        gate = self.live.state
        if face == "approved":
            return {"passed": "approved", "spoof": "spoof"}.get(gate, "liveness")
        if face == "scanning" and gate == "challenge":
            return "liveness"  # identity dips briefly while turning the head
        return face

    def _new_gate(self) -> LivenessGate:
        s = self.s
        return LivenessGate(
            LivenessConfig(
                challenge_steps=s.liveness_challenge_steps,
                step_timeout_s=s.liveness_step_timeout_s,
                pass_threshold=s.liveness_pass_threshold,
                spoof_threshold=s.liveness_spoof_threshold,
                rechallenge_min_s=s.liveness_recheck_min_s,
                rechallenge_max_s=s.liveness_recheck_max_s,
                blink_gap_s=s.liveness_blink_gap_s,
                lost_reset_s=s.liveness_lost_reset_s,
            )
        )

    def _new_auth(self) -> ContinuousFaceAuth:
        return ContinuousFaceAuth(
            threshold=self.s.face_threshold,
            reject_threshold=self.s.face_reject_threshold,
            window=self.s.face_window,
            reauth_interval_s=self.s.reauth_interval_s,
            absence_lock_s=self.s.absence_lock_s,
        )

    # -- profile -----------------------------------------------------------
    @property
    def owner_name(self) -> str:
        return self.db.get("owner_name", "") or ""

    @property
    def assistant_name(self) -> str:
        return self.db.get("assistant_name", "JARVIS") or "JARVIS"

    @property
    def setup_complete(self) -> bool:
        return self.db.get("setup_complete") == "1"

    @property
    def face_enrolled(self) -> bool:
        return self.store.exists(FACE)

    @property
    def voice_enrolled(self) -> bool:
        return self.voice is not None and self.voice.enrolled

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.bus.log("Core systems online")
        if self.engine.load():
            self.bus.log("Face recognition model loaded")
        else:
            self.bus.log(self.engine.error or "Face model unavailable", "error")
        self.camera.start()
        if self.voice is not None:
            self.voice.start()
        if self.setup_complete and self.face_enrolled:
            self.begin_verification()
        self._thread = threading.Thread(target=self._loop, name="face-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.camera.stop()
        if self.voice is not None:
            self.voice.stop()

    # -- modes ---------------------------------------------------------------
    def begin_enrollment(self) -> None:
        with self._lock:
            self.enrollment = EnrollmentSession(
                min_quality=self.s.min_face_quality,
                min_live_score=self.s.liveness_spoof_threshold if self.s.liveness_enabled else None,
            )
            self.mode = "enrolling"
        self.bus.log("Face enrollment started")

    def cancel_enrollment(self) -> None:
        with self._lock:
            self.enrollment = None
            self.mode = "verifying" if self.verifier else "idle"
        self.bus.log("Face enrollment cancelled", "warn")

    def begin_verification(self) -> bool:
        try:
            template = self.store.load(FACE)
        except Exception:
            log.exception("could not decrypt face template")
            self.bus.log("Face profile could not be decrypted", "error")
            return False
        if template is None:
            return False
        with self._lock:
            self.verifier = TemplateMatcher(template, top_k=self.s.face_top_k)
            self.auth = self._new_auth()
            self.live = self._new_gate()
            self._unlocked = False
            self.mode = "verifying"
        self.bus.log("Continuous face verification active")
        if self.voice is not None and self.voice.enrolled:
            self.voice.begin_verification()
        return True

    # -- main loop -----------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            # blinks last ~150 ms: analyse faster while a challenge is running
            fps = self.s.challenge_fps if self.live.state == "challenge" else self.s.process_fps
            period = 1.0 / max(fps, 1.0)
            t0 = time.monotonic()
            try:
                self._tick(t0)
            except Exception:
                log.exception("face loop tick failed")
            self._stop.wait(max(0.0, period - (time.monotonic() - t0)))

    def _tick(self, now: float) -> None:
        frame = self.camera.latest()
        if frame is None or not self.engine.ready or self.mode == "idle":
            if self.mode == "idle" and frame is not None and self.bus.has_subscribers:
                self._push_preview(frame, [])
            return
        if self.mode == "verifying":
            frozen = self.frozen.update(frame, now)
            if frozen and not self._frozen:
                self.db.add_security_event(
                    "camera_frozen", "Camera delivered identical frames — virtual or replayed feed?", blocked=True
                )
                self.bus.log("Camera feed frozen — not accepted as live", "alert")
            self._frozen = frozen
        faces = self.engine.analyze(frame)
        if self.bus.has_subscribers:
            self._push_preview(frame, faces)
        if self.mode == "enrolling":
            self._tick_enrollment(faces, now)
        elif self.mode == "verifying":
            self._tick_verification(faces, now)

    def _tick_enrollment(self, faces: list[FaceObservation], now: float) -> None:
        with self._lock:
            session = self.enrollment
        if session is None:
            return
        session.update(faces, now)
        self.bus.publish({"type": "enroll", **session.snapshot()})
        if session.done:
            template = session.template()
            self.store.save(FACE, template)
            health = TemplateMatcher(template, self.s.face_top_k).self_consistency()
            with self._lock:
                self.enrollment = None
                self.mode = "idle"
            log.info("face template saved: %d samples, self-consistency %.3f", len(template), health)
            self.bus.log(f"Face profile saved ({len(template)} encrypted samples)")
            self.bus.publish({"type": "enroll_complete"})

    def _tick_verification(self, faces: list[FaceObservation], now: float) -> None:
        verifier = self.verifier
        if verifier is None:
            return
        usable = [f for f in faces if f.quality >= self.s.min_face_quality]
        sims = [verifier.similarity(f.embedding) for f in usable]
        prev = self.auth.state
        prev_effective = self.effective_state()
        events = self.auth.update(
            FrameResult(similarities=sims, low_quality_faces=len(faces) - len(usable)), now
        )
        for kind, detail in events:
            self._handle_event(kind, detail, prev, sims)

        live_events: list[tuple[str, str]] = []
        if self.s.liveness_enabled:
            obs = None
            if sims and max(sims) >= self.s.face_reject_threshold:
                best = usable[int(np.argmax(sims))]
                obs = LiveObs(
                    yaw=best.yaw,
                    rel_width=best.rel_width,
                    center=best.center,
                    eye_open=best.eye_open,
                    live_score=best.live_score,
                )
            live_events = self.live.update(self.auth.state, obs, now, frozen=self._frozen)
            for kind, detail in live_events:
                self._handle_liveness(kind, detail, sims)

        changed = self.effective_state() != prev_effective
        # challenges push faster so the countdown and hints stay live
        interval = 0.25 if self.live.state == "challenge" else 1.0
        if events or live_events or changed or now - self._last_state_push > interval:
            self._push_state()

    def _handle_event(self, kind: str, detail: str, prev: str, sims: list[float]) -> None:
        if kind == "state_approved":
            if not self.s.liveness_enabled:
                self.bus.log("Face verified — owner identified", "ok")
                if prev != "approved":
                    self._greet()
            elif self.live.state != "passed":
                self.bus.log("Face matched — verifying liveness")
        elif kind == "state_denied":
            best = max(sims) if sims else None
            self.db.add_security_event(
                "unknown_face",
                "Unrecognised person in front of the camera",
                face_conf=None if best is None else round(confidence(best, self.s.face_threshold), 3),
                blocked=True,
            )
            self.bus.log("Unknown person detected — access denied", "alert")
            self.bus.publish(
                {"type": "say", "text": "Authentication failed. You are not my boss."}
            )
        elif kind == "state_absent":
            self._unlocked = False
            self.bus.log("Owner left — session locked", "warn")
        elif kind == "state_scanning":
            self.bus.log(detail)
        elif kind == "bystander":
            self.db.add_security_event("bystander", detail)
            self.bus.log("Unknown person in view with owner", "warn")

    def _greet(self) -> None:
        self._unlocked = True
        greeting = f"Authentication approved. Hi {self.owner_name}, how may I help you today?"
        self.bus.publish({"type": "say", "text": greeting})

    def _face_conf(self, sims: list[float]) -> float | None:
        return round(confidence(max(sims), self.s.face_threshold), 3) if sims else None

    def _handle_liveness(self, kind: str, detail: str, sims: list[float]) -> None:
        if kind == "liveness_challenge":
            self.bus.log(f"{detail} — challenge issued")
            self.bus.publish({"type": "say", "text": "Quick liveness check. Follow the prompts."})
        elif kind == "liveness_passed":
            self.bus.log("Liveness confirmed — live person", "ok")
            if not self._unlocked:
                self._greet()
        elif kind == "liveness_failed":
            self.db.add_security_event(
                "liveness_failed", detail, face_conf=self._face_conf(sims), blocked=True
            )
            self.bus.log(f"Liveness check failed: {detail}", "alert")
        elif kind == "liveness_lockout":
            self.db.add_security_event("liveness_lockout", detail, blocked=True)
            self.bus.log(detail, "alert")
            self.bus.publish(
                {"type": "say", "text": "Too many failed liveness checks. Access is locked for a minute."}
            )
        elif kind == "spoof":
            self._unlocked = False
            self.db.add_security_event(
                "spoof_suspected", detail, face_conf=self._face_conf(sims), blocked=True
            )
            self.bus.log(f"Spoof suspected: {detail}", "alert")
            self.bus.publish({"type": "say", "text": "Spoof attempt detected. Access denied."})
        elif kind == "liveness_reset":
            self._unlocked = False

    # -- outputs -------------------------------------------------------------
    def auth_public(self) -> dict:
        """Auth state safe to show anyone sitting at the screen."""
        snap = self.auth.snapshot()
        state = self.effective_state()
        owner = state == "approved"
        out = {
            "state": state,
            "reason": self.live.reason if state in ("liveness", "spoof") else snap.reason,
            "faces": snap.faces,
        }
        if owner:
            # values are only revealed to the verified owner
            out["face_confidence"] = (
                None
                if snap.confidence_sim is None
                else round(confidence(snap.confidence_sim, self.s.face_threshold), 3)
            )
            out["bystander"] = snap.bystander
        if self.s.liveness_enabled:
            out["liveness"] = self.live.public(time.monotonic(), owner)
        else:
            out["liveness"] = {"state": "disabled", "reason": "Liveness checks are turned off"}
        if self.voice is not None:
            out["voice"] = self.voice.public(owner_verified=owner)
        return out

    def _push_state(self) -> None:
        self._last_state_push = time.monotonic()
        self.bus.publish({"type": "auth", **self.auth_public()})

    def _push_preview(self, frame: np.ndarray, faces: list[FaceObservation]) -> None:
        h, w = frame.shape[:2]
        scale = self.s.preview_width / w
        small = cv2.resize(frame, (self.s.preview_width, int(h * scale)))
        ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 60])
        if not ok:
            return
        boxes = [[float(v) for v in (f.bbox / np.array([w, h, w, h]))] for f in faces]
        self.bus.publish(
            {
                "type": "preview",
                "jpeg": base64.b64encode(jpg.tobytes()).decode(),
                "boxes": boxes,
            }
        )

    def _liveness_status(self) -> str:
        if not self.s.liveness_enabled:
            return "disabled"
        passive = getattr(self.engine, "liveness", None)
        if passive is None or not passive.ready:
            # challenges still run; only the texture model is missing
            return "challenges only — " + (getattr(passive, "error", None) or "passive model not loaded")
        return "ready"

    def status(self) -> dict:
        return {
            "setup_complete": self.setup_complete,
            "owner_name": self.owner_name,
            "assistant_name": self.assistant_name,
            "face_enrolled": self.face_enrolled,
            "voice_enrolled": self.voice_enrolled,
            "mode": self.mode,
            "voice_mode": self.voice.mode if self.voice else "unavailable",
            "camera": {"status": self.camera.status, "error": self.camera.error},
            "mic": (
                {"status": self.voice.mic.status, "error": self.voice.mic.error, "device": self.voice.mic.device_name}
                if self.voice
                else {"status": "off", "error": "voice pipeline disabled", "device": None}
            ),
            "models": {
                "face": "ready" if self.engine.ready else (self.engine.error or "not loaded"),
                "voice": self.voice.model_status() if self.voice else "disabled",
                "liveness": self._liveness_status(),
                "llm": "not_implemented",
                "stt": "not_implemented",
                "tts": "not_implemented",
            },
            "auth": self.auth_public(),
        }
