"""Randomised challenge-response liveness.

A challenge is a short random sequence of actions. "Blink twice" is always
included because a photo can be tilted or moved closer but can't blink; the
other actions and the order are random, so a pre-recorded video won't match.
Each step has a deadline, and the face must stay continuous (no jump to a
different position or size between frames, as when swapping a phone in).

Direction convention: frames come unmirrored from the camera, so when the
person turns towards their own left, the nose moves to the image's right and
InsightFace reports a positive yaw.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

TURN_MIN_YAW = 18.0  # degrees away from frontal
TURN_MIN_DELTA = 12.0  # degrees of actual movement during the step
CLOSER_RATIO = 1.25
BLINKS_NEEDED = 2
MAX_CENTER_JUMP = 0.6  # face widths per frame
MAX_SIZE_JUMP = 1.5  # size ratio per frame

PROMPTS = {
    "blink": "Blink twice",
    "turn_left": "Turn your head to your left",
    "turn_right": "Turn your head to your right",
    "closer": "Move closer to the camera",
}
EXTRA_STEPS = ["turn_left", "turn_right", "closer"]


@dataclass
class LiveObs:
    """The owner-candidate face in one frame, as liveness sees it."""

    yaw: float
    rel_width: float  # face width / frame width
    center: tuple[float, float]  # normalised to frame size
    eye_open: float | None
    live_score: float | None


def random_steps(rng: random.Random, count: int) -> list[str]:
    count = max(1, min(count, 1 + len(EXTRA_STEPS)))
    steps = ["blink"] + rng.sample(EXTRA_STEPS, count - 1)
    rng.shuffle(steps)
    return steps


class ChallengeSession:
    def __init__(self, steps: list[str], step_timeout_s: float, now: float):
        self.steps = steps
        self.step_timeout_s = step_timeout_s
        self.index = 0
        self.hint = ""
        self.failure: str | None = None
        self._step_t = now
        self._base: LiveObs | None = None
        self._prev: LiveObs | None = None
        self._blinks = 0

    @property
    def done(self) -> bool:
        return self.index >= len(self.steps)

    @property
    def step(self) -> str | None:
        return None if self.done else self.steps[self.index]

    def _advance(self, now: float) -> None:
        self.index += 1
        self._step_t = now
        self._base = None
        self._blinks = 0
        self.hint = ""

    def _fail(self, reason: str) -> str:
        self.failure = reason
        return "failed"

    def update(self, obs: LiveObs | None, now: float, blinked: bool) -> str:
        """Feed one frame. Returns "running", "passed" or "failed"."""
        if self.failure:
            return "failed"
        if self.done:
            return "passed"
        step = self.steps[self.index]
        if now - self._step_t > self.step_timeout_s:
            return self._fail(f"Timed out waiting for: {PROMPTS[step].lower()}")
        if obs is None:
            self.hint = "I can't see your face"
            self._prev = None
            return "running"

        prev, self._prev = self._prev, obs
        if prev is not None:
            jump = ((obs.center[0] - prev.center[0]) ** 2 + (obs.center[1] - prev.center[1]) ** 2) ** 0.5
            size = max(obs.rel_width, prev.rel_width) / max(min(obs.rel_width, prev.rel_width), 1e-6)
            if jump / max(prev.rel_width, 1e-6) > MAX_CENTER_JUMP or size > MAX_SIZE_JUMP:
                return self._fail("The face changed during the check")

        if self._base is None:
            self._base = obs
        base = self._base

        if step == "blink":
            if blinked:
                self._blinks += 1
            if self._blinks >= BLINKS_NEEDED:
                self._advance(now)
            else:
                self.hint = f"{self._blinks} of {BLINKS_NEEDED} — blink slowly"
        elif step in ("turn_left", "turn_right"):
            sign = 1.0 if step == "turn_left" else -1.0
            yaw = obs.yaw * sign
            if yaw >= TURN_MIN_YAW and yaw - base.yaw * sign >= TURN_MIN_DELTA:
                self._advance(now)
            elif -yaw >= TURN_MIN_YAW:
                self.hint = "Other way"
            else:
                self.hint = "A little further"
        elif step == "closer":
            if obs.rel_width >= base.rel_width * CLOSER_RATIO:
                self._advance(now)
            else:
                self.hint = "A bit closer"
        return "passed" if self.done else "running"

    def snapshot(self, now: float) -> dict:
        step = self.step
        return {
            "step": step or "done",
            "prompt": PROMPTS[step] if step else "Check complete",
            "hint": self.hint,
            "step_index": self.index,
            "step_count": len(self.steps),
            "steps": list(self.steps),
            "remaining_s": round(max(0.0, self.step_timeout_s - (now - self._step_t)), 1),
        }
