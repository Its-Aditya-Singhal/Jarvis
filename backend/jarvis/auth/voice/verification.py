"""Per-utterance voice verification with a short validity window (inference side)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class VoiceResult:
    verdict: str  # verified | uncertain | rejected
    similarity: float
    quality: float
    t: float


@dataclass
class VoiceAuth:
    threshold: float
    reject_threshold: float
    valid_s: float = 20.0
    min_quality: float = 0.3
    last: VoiceResult | None = None

    def judge(self, similarity: float, quality: float, now: float) -> VoiceResult:
        if quality < self.min_quality:
            verdict = "uncertain"  # never reject someone because of a noisy room
        elif similarity >= self.threshold:
            verdict = "verified"
        elif similarity < self.reject_threshold:
            verdict = "rejected"
        else:
            verdict = "uncertain"
        self.last = VoiceResult(verdict, similarity, quality, now)
        return self.last

    def state(self, now: float) -> str:
        """idle | verified | uncertain | rejected; results expire after ``valid_s``."""
        if self.last is None or now - self.last.t > self.valid_s:
            return "idle"
        return self.last.verdict

    def reset(self) -> None:
        self.last = None
