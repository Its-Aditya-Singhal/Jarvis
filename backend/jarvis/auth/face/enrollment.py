"""Guided face enrollment (training side).

The user is walked through poses/expressions. A step only collects samples
once the requested pose is actually observed (head pose from the 3D landmark
model, distance from face size, smile from mouth/eye geometry). Only the
embeddings are kept; frames are never stored.

Left/right and up/down are sign-agnostic: whichever direction the user turns
first for "left" defines the sign, and "right" then requires the opposite.
That makes enrollment independent of camera mirroring and pose conventions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from .types import FaceObservation

YAW_TURN = 14.0  # degrees
PITCH_TURN = 9.0
FRONTAL_MAX = 12.0
CLOSER_RATIO = 1.22
FARTHER_RATIO = 0.82
SMILE_RATIO = 1.07


@dataclass
class Step:
    key: str
    prompt: str


STEPS: list[Step] = [
    Step("straight", "Look straight at the camera"),
    Step("left", "Turn your head slightly left"),
    Step("right", "Now turn slightly right"),
    Step("up", "Look slightly up"),
    Step("down", "Look slightly down"),
    Step("closer", "Move a little closer"),
    Step("farther", "Now move back a little"),
    Step("smile", "Give me a smile"),
    Step("neutral", "Relax — neutral expression"),
]


def mouth_eye_ratio(landmarks: np.ndarray | None) -> float | None:
    """Mouth width relative to outer-eye span (68-point indexing)."""
    if landmarks is None or len(landmarks) < 68:
        return None
    mouth = np.linalg.norm(landmarks[54] - landmarks[48])
    eyes = np.linalg.norm(landmarks[45] - landmarks[36])
    return float(mouth / eyes) if eyes > 1e-6 else None


@dataclass
class EnrollmentSession:
    samples_per_step: int = 4
    min_interval_s: float = 0.3
    min_quality: float = 0.35
    step_index: int = 0
    embeddings: list[np.ndarray] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    _step_count: int = 0
    _last_sample_t: float = 0.0
    # baselines measured during the "straight" step
    _base_rel_width: list[float] = field(default_factory=list)
    _base_smile: list[float] = field(default_factory=list)
    _yaw_sign: float = 0.0
    _pitch_sign: float = 0.0
    hint: str = ""

    @property
    def total(self) -> int:
        return len(STEPS) * self.samples_per_step

    @property
    def done(self) -> bool:
        return self.step_index >= len(STEPS)

    @property
    def progress(self) -> float:
        return min(len(self.embeddings) / self.total, 1.0)

    @property
    def current(self) -> Step | None:
        return None if self.done else STEPS[self.step_index]

    def _pose_ok(self, key: str, o: FaceObservation) -> tuple[bool, str]:
        base_w = float(np.median(self._base_rel_width)) if self._base_rel_width else None
        base_smile = float(np.median(self._base_smile)) if self._base_smile else None
        smile = mouth_eye_ratio(o.landmarks)
        frontal = abs(o.yaw) < FRONTAL_MAX and abs(o.pitch) < FRONTAL_MAX

        if key == "straight":
            return frontal, "" if frontal else "Face the camera directly"
        if key in ("left", "right"):
            if abs(o.yaw) < YAW_TURN:
                return False, "Turn a little further"
            sign = float(np.sign(o.yaw))
            if key == "left":
                return True, ""
            return (sign == -self._yaw_sign), "" if sign == -self._yaw_sign else "Other way"
        if key in ("up", "down"):
            if abs(o.pitch) < PITCH_TURN:
                return False, "Tilt a little more"
            sign = float(np.sign(o.pitch))
            if key == "up":
                return True, ""
            return (sign == -self._pitch_sign), "" if sign == -self._pitch_sign else "Other way"
        if key == "closer":
            ok = base_w is not None and o.rel_width > base_w * CLOSER_RATIO
            return ok, "" if ok else "A bit closer"
        if key == "farther":
            ok = base_w is not None and o.rel_width < base_w * FARTHER_RATIO
            return ok, "" if ok else "A bit farther back"
        if key == "smile":
            ok = base_smile is not None and smile is not None and smile > base_smile * SMILE_RATIO
            return ok, "" if ok else "Bigger smile"
        if key == "neutral":
            ok = frontal and (
                base_smile is None or smile is None or smile < base_smile * (SMILE_RATIO - 0.02)
            )
            return ok, "" if ok else "Relax your face and look straight"
        return False, ""

    def update(self, faces: list[FaceObservation], now: float | None = None) -> bool:
        """Feed one processed frame. Returns True if a sample was captured."""
        if self.done:
            return False
        now = time.monotonic() if now is None else now
        if not faces:
            self.hint = "I can't see your face"
            return False
        if len(faces) > 1:
            self.hint = "Only you should be in frame"
            return False
        o = faces[0]
        if o.quality < self.min_quality:
            self.hint = "Hold still — improve lighting if possible"
            return False
        step = STEPS[self.step_index]
        ok, hint = self._pose_ok(step.key, o)
        self.hint = hint
        if not ok or now - self._last_sample_t < self.min_interval_s:
            return False

        if step.key == "straight":
            self._base_rel_width.append(o.rel_width)
            r = mouth_eye_ratio(o.landmarks)
            if r is not None:
                self._base_smile.append(r)
        if step.key == "left" and self._yaw_sign == 0.0:
            self._yaw_sign = float(np.sign(o.yaw))
        if step.key == "up" and self._pitch_sign == 0.0:
            self._pitch_sign = float(np.sign(o.pitch))

        self.embeddings.append(o.embedding.copy())
        self.labels.append(step.key)
        self._last_sample_t = now
        self._step_count += 1
        if self._step_count >= self.samples_per_step:
            self.step_index += 1
            self._step_count = 0
        return True

    def template(self) -> np.ndarray:
        return np.stack(self.embeddings).astype(np.float32)

    def snapshot(self) -> dict:
        cur = self.current
        return {
            "step": cur.key if cur else "done",
            "prompt": cur.prompt if cur else "Face enrollment complete",
            "hint": self.hint,
            "step_index": self.step_index,
            "step_count": len(STEPS),
            "progress": round(self.progress, 3),
            "done": self.done,
            "steps": [s.key for s in STEPS],
        }
