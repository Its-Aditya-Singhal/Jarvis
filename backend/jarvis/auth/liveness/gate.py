"""Session liveness state.

States:
  idle       — no live proof yet (starts a challenge once the face matches)
  challenge  — randomised challenge in progress
  cooldown   — short pause after a failed challenge (long after repeated ones)
  passed     — liveness proven for this presence session
  spoof      — the matched face looks like a photo/screen or the feed is frozen

Liveness is lost when the owner leaves (face absent/denied or out of view for
``lost_reset_s``). While passed, a fresh challenge is demanded at a random
time, early if no natural blink has been seen for ``blink_gap_s``, and at
once if the passive score sits below the pass threshold (ambiguous texture).
Time and randomness are injected so this is unit-testable without a camera.
"""

from __future__ import annotations

import random
import statistics
from collections import deque
from dataclasses import dataclass

from .blink import BlinkDetector
from .challenges import ChallengeSession, LiveObs, PROMPTS, random_steps


@dataclass
class LivenessConfig:
    challenge_steps: int = 2
    step_timeout_s: float = 8.0
    pass_threshold: float = 0.6  # median passive score needed to pass a challenge
    spoof_threshold: float = 0.25  # sustained median below this = spoof
    spoof_window: int = 10
    rechallenge_min_s: float = 300.0
    rechallenge_max_s: float = 900.0
    blink_gap_s: float = 150.0
    lost_reset_s: float = 5.0
    retry_cooldown_s: float = 3.0
    max_failures: int = 3
    lockout_s: float = 60.0


class LivenessGate:
    def __init__(self, cfg: LivenessConfig, rng: random.Random | None = None):
        self.cfg = cfg
        self.rng = rng or random.SystemRandom()
        self.blink = BlinkDetector()
        self.state = "idle"
        self.reason = "Liveness not yet proven"
        self.session: ChallengeSession | None = None
        self.passed_t: float | None = None
        self.next_check_t: float | None = None
        self.failures = 0
        self._scores: deque[float] = deque(maxlen=cfg.spoof_window)
        self._challenge_scores: list[float] = []
        self._until = 0.0
        self._last_seen: float | None = None
        self._last_blink: float | None = None

    # -- helpers -------------------------------------------------------------
    @property
    def live_score(self) -> float | None:
        return statistics.median(self._scores) if self._scores else None

    def _spoofed(self) -> bool:
        return (
            len(self._scores) >= max(3, self.cfg.spoof_window // 2)
            and statistics.median(self._scores) < self.cfg.spoof_threshold
        )

    def _go(self, state: str, reason: str) -> None:
        self.state = state
        self.reason = reason
        if state != "challenge":
            self.session = None

    def reset(self, reason: str = "Liveness not yet proven") -> None:
        self._go("idle", reason)
        self.passed_t = None
        self.next_check_t = None
        self._scores.clear()
        self.blink.reset()

    def _start_challenge(self, now: float, reason: str) -> tuple[str, str]:
        steps = random_steps(self.rng, self.cfg.challenge_steps)
        self.session = ChallengeSession(steps, self.cfg.step_timeout_s, now)
        self._challenge_scores = []
        self.state = "challenge"
        self.reason = reason
        return ("liveness_challenge", reason)

    def _fail(self, now: float, reason: str) -> list[tuple[str, str]]:
        self.failures += 1
        events = [("liveness_failed", reason)]
        if self.failures >= self.cfg.max_failures:
            self.failures = 0
            self._until = now + self.cfg.lockout_s
            self._go("cooldown", f"Too many failed checks — locked for {int(self.cfg.lockout_s)} s")
            events.append(("liveness_lockout", self.reason))
        else:
            self._until = now + self.cfg.retry_cooldown_s
            self._go("cooldown", reason)
        self.passed_t = None
        return events

    # -- main update -----------------------------------------------------------
    def update(
        self, face_state: str, obs: LiveObs | None, now: float, frozen: bool = False
    ) -> list[tuple[str, str]]:
        """Advance with one processed frame. ``obs`` is the best-matching face."""
        events: list[tuple[str, str]] = []
        blinked = False
        if obs is not None:
            self._last_seen = now
            if obs.live_score is not None:
                self._scores.append(obs.live_score)
                if self.state == "challenge":
                    self._challenge_scores.append(obs.live_score)
            blinked = self.blink.update(obs.eye_open, now)
            if blinked:
                self._last_blink = now

        # the owner (or whatever matched) went away: liveness must be re-proven
        gone = face_state in ("denied", "absent", "no_profile") or (
            self.state in ("passed", "challenge", "spoof")
            and (self._last_seen is None or now - self._last_seen > self.cfg.lost_reset_s)
        )
        if gone:
            if self.state in ("passed", "challenge", "spoof"):
                self.reset("Owner out of view — liveness must be re-proven")
                events.append(("liveness_reset", self.reason))
            if self.state == "cooldown" and now >= self._until:
                self._go("idle", "Liveness not yet proven")
            return events

        if self.state == "cooldown":
            if now < self._until:
                return events
            self._go("idle", "Liveness not yet proven")

        if self.state in ("idle", "challenge", "passed"):
            if frozen:
                self._go("spoof", "Camera feed is frozen — possible replayed video")
                self.passed_t = None
                return events + [("spoof", self.reason)]
            if self._spoofed() and (self.state != "idle" or face_state == "approved"):
                self._go("spoof", "Photo, screen or mask suspected")
                self.passed_t = None
                return events + [("spoof", self.reason)]

        if self.state == "spoof":
            # a false alarm (e.g. harsh lighting) clears once the texture score is
            # convincingly live for a full window; the person must then pass a challenge
            if (
                not frozen
                and len(self._scores) == self.cfg.spoof_window
                and min(self._scores) >= self.cfg.pass_threshold
            ):
                self._go("idle", "Liveness not yet proven")
            return events

        if self.state == "idle":
            if face_state == "approved":
                events.append(self._start_challenge(now, "Prove you're live"))
            return events

        if self.state == "challenge":
            assert self.session is not None
            result = self.session.update(obs, now, blinked)
            if result == "failed":
                return events + self._fail(now, self.session.failure or "Challenge failed")
            if result == "passed":
                if face_state != "approved":
                    self.session.hint = "Look at the camera"
                    return events
                scores = self._challenge_scores
                if scores and statistics.median(scores) < self.cfg.pass_threshold:
                    return events + self._fail(now, "Face texture check failed — photo or screen suspected")
                self.failures = 0
                self.passed_t = now
                self._last_blink = now
                self.next_check_t = now + self.rng.uniform(
                    self.cfg.rechallenge_min_s, self.cfg.rechallenge_max_s
                )
                self._go("passed", "Live person confirmed")
                events.append(("liveness_passed", self.reason))
            return events

        # passed
        if face_state == "approved":
            if (
                len(self._scores) == self.cfg.spoof_window
                and statistics.median(self._scores) < self.cfg.pass_threshold
            ):
                # ambiguous texture (not clearly a spoof): make the person prove it
                events.append(self._start_challenge(now, "Anti-spoof score dropped — liveness check"))
            elif self.next_check_t is not None and now >= self.next_check_t:
                events.append(self._start_challenge(now, "Routine liveness check"))
            elif self._last_blink is not None and now - self._last_blink > self.cfg.blink_gap_s:
                events.append(self._start_challenge(now, "No natural blinking seen — liveness check"))
        return events

    # -- output ----------------------------------------------------------------
    def public(self, now: float, owner: bool) -> dict:
        out: dict = {"state": self.state, "reason": self.reason}
        if self.session is not None:
            out["challenge"] = self.session.snapshot(now)
        if self.state == "cooldown":
            out["cooldown_s"] = round(max(0.0, self._until - now), 1)
        if owner:
            score = self.live_score
            out["live_score"] = None if score is None else round(score, 3)
            out["checked_ago"] = None if self.passed_t is None else round(now - self.passed_t, 1)
            out["next_check_s"] = (
                None if self.next_check_t is None else round(max(0.0, self.next_check_t - now), 1)
            )
        return out


__all__ = ["LivenessConfig", "LivenessGate", "LiveObs", "PROMPTS"]
