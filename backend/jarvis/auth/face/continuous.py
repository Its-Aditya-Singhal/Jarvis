"""Continuous face authentication state machine (inference side).

Consumes per-frame similarities and decides, over a short smoothing window,
whether the owner is present. Approval expires unless it keeps being renewed,
so one successful match is never permanent trust. Time is injected so the
logic is unit-testable without a camera.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field


@dataclass
class FrameResult:
    similarities: list[float]  # one per usable face
    low_quality_faces: int = 0


@dataclass
class AuthSnapshot:
    state: str  # no_profile | scanning | approved | denied | absent
    owner_present: bool
    confidence_sim: float | None  # owner-facing only; never sent while not approved
    faces: int
    bystander: bool
    reason: str


@dataclass
class ContinuousFaceAuth:
    threshold: float
    reject_threshold: float
    window: int = 6
    reauth_interval_s: float = 30.0
    absence_lock_s: float = 8.0
    min_frames: int = 3
    hold_margin: float = 0.07  # hysteresis: stay approved slightly below threshold

    state: str = "scanning"
    last_verified_t: float | None = None
    last_owner_seen_t: float | None = None
    _scores: deque = field(default_factory=deque)
    _bystander: bool = False
    _faces: int = 0
    _reason: str = "Looking for you"
    _smoothed: float | None = None

    def reset(self) -> None:
        self.state = "scanning"
        self.last_verified_t = None
        self.last_owner_seen_t = None
        self._scores.clear()
        self._smoothed = None
        self._reason = "Looking for you"

    def update(self, frame: FrameResult, now: float) -> list[tuple[str, str]]:
        """Advance the state machine. Returns (event_kind, detail) transitions."""
        prev = self.state
        events: list[tuple[str, str]] = []
        sims = frame.similarities
        had_bystander = self._bystander
        self._faces = len(sims) + frame.low_quality_faces

        if not sims:
            self._bystander = False
            if self.state == "approved" and self.last_owner_seen_t is not None:
                if now - self.last_owner_seen_t > self.absence_lock_s:
                    self.state = "absent"
                    self._reason = "Owner left — session locked"
                    self._scores.clear()
            elif self.state in ("scanning", "denied"):
                if frame.low_quality_faces:
                    self._reason = "Face unclear — hold still"
                else:
                    self._reason = "No face detected"
                    if self.state == "denied":
                        self.state = "scanning"
                        self._scores.clear()
        else:
            best = max(sims)
            owner_here = best >= self.threshold
            self._bystander = owner_here and any(s < self.reject_threshold for s in sims)
            self._scores.append(best)
            while len(self._scores) > self.window:
                self._scores.popleft()
            enough = len(self._scores) >= self.min_frames
            med = statistics.median(self._scores)
            self._smoothed = med

            if owner_here:
                self.last_owner_seen_t = now
            if enough and med >= self.threshold:
                self.state = "approved"
                self.last_verified_t = now
                self._reason = "Owner verified"
            elif enough and med < self.reject_threshold:
                self.state = "denied"
                self._reason = "Unrecognised person"
            elif self.state == "approved" and med >= self.threshold - self.hold_margin:
                pass  # brief dip (head turn, blur): keep approval until it expires
            elif enough or self.state != "approved":
                self.state = "scanning"
                self._reason = "Verifying identity"

        # approval decays without fresh evidence
        if (
            self.state == "approved"
            and self.last_verified_t is not None
            and now - self.last_verified_t > self.reauth_interval_s
        ):
            self.state = "scanning"
            self._reason = "Re-verification required"

        if self.state != prev:
            events.append((f"state_{self.state}", self._reason))
        if self._bystander and not had_bystander:
            events.append(("bystander", "Unknown person in view with owner"))
        return events

    def snapshot(self) -> AuthSnapshot:
        return AuthSnapshot(
            state=self.state,
            owner_present=self.state == "approved",
            confidence_sim=self._smoothed if self.state == "approved" else None,
            faces=self._faces,
            bystander=self._bystander,
            reason=self._reason,
        )
