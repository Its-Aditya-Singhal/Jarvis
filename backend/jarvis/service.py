"""Orchestrates camera -> face engine -> enrollment / continuous verification."""

from __future__ import annotations

import base64
import logging
import threading
import time

import cv2
import numpy as np

from .auth.face.continuous import ContinuousFaceAuth, FrameResult
from .auth.face.engine import FaceEngine
from .auth.face.enrollment import EnrollmentSession
from .auth.face.types import FaceObservation
from .auth.face.verifier import FaceVerifier, confidence
from .camera.capture import Camera
from .config import Settings
from .database.db import Database
from .events import EventBus
from .security.template_store import TemplateStore

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
    ):
        self.s = settings
        self.db = db
        self.store = store
        self.bus = bus
        self.engine = engine
        self.camera = camera
        self.mode = "idle"  # idle | enrolling | verifying
        self.enrollment: EnrollmentSession | None = None
        self.verifier: FaceVerifier | None = None
        self.auth = self._new_auth()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_state_push = 0.0
        self._lock = threading.Lock()

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

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.bus.log("Core systems online")
        if self.engine.load():
            self.bus.log("Face recognition model loaded")
        else:
            self.bus.log(self.engine.error or "Face model unavailable", "error")
        self.camera.start()
        if self.setup_complete and self.face_enrolled:
            self.begin_verification()
        self._thread = threading.Thread(target=self._loop, name="face-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.camera.stop()

    # -- modes ---------------------------------------------------------------
    def begin_enrollment(self) -> None:
        with self._lock:
            self.enrollment = EnrollmentSession(min_quality=self.s.min_face_quality)
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
            self.verifier = FaceVerifier(template, top_k=self.s.face_top_k)
            self.auth = self._new_auth()
            self.mode = "verifying"
        self.bus.log("Continuous face verification active")
        return True

    # -- main loop -----------------------------------------------------------
    def _loop(self) -> None:
        period = 1.0 / max(self.s.process_fps, 1.0)
        while not self._stop.is_set():
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
            health = FaceVerifier(template, self.s.face_top_k).self_consistency()
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
        events = self.auth.update(
            FrameResult(similarities=sims, low_quality_faces=len(faces) - len(usable)), now
        )
        for kind, detail in events:
            self._handle_event(kind, detail, prev, sims)
        if events or now - self._last_state_push > 1.0:
            self._push_state()

    def _handle_event(self, kind: str, detail: str, prev: str, sims: list[float]) -> None:
        if kind == "state_approved":
            self.bus.log("Face verified — owner identified", "ok")
            if prev != "approved":
                greeting = (
                    f"Authentication approved. Hi {self.owner_name}, how may I help you today?"
                )
                self.bus.publish({"type": "say", "text": greeting})
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
            self.bus.log("Owner left — session locked", "warn")
        elif kind == "state_scanning":
            self.bus.log(detail)
        elif kind == "bystander":
            self.db.add_security_event("bystander", detail)
            self.bus.log("Unknown person in view with owner", "warn")

    # -- outputs -------------------------------------------------------------
    def auth_public(self) -> dict:
        """Auth state safe to show anyone sitting at the screen."""
        snap = self.auth.snapshot()
        out = {
            "state": snap.state if self.mode == "verifying" else "no_profile",
            "reason": snap.reason,
            "faces": snap.faces,
        }
        if snap.state == "approved":
            # values are only revealed to the verified owner
            out["face_confidence"] = (
                None
                if snap.confidence_sim is None
                else round(confidence(snap.confidence_sim, self.s.face_threshold), 3)
            )
            out["bystander"] = snap.bystander
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

    def status(self) -> dict:
        return {
            "setup_complete": self.setup_complete,
            "owner_name": self.owner_name,
            "assistant_name": self.assistant_name,
            "face_enrolled": self.face_enrolled,
            "mode": self.mode,
            "camera": {"status": self.camera.status, "error": self.camera.error},
            "models": {
                "face": "ready" if self.engine.ready else (self.engine.error or "not loaded"),
                "voice": "not_implemented",
                "liveness": "not_implemented",
                "llm": "not_implemented",
                "stt": "not_implemented",
                "tts": "not_implemented",
            },
            "auth": self.auth_public(),
        }
