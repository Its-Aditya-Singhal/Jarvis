"""Guided voice enrollment (training side).

The owner reads short phrases in English, Hindi and Hinglish. Each accepted
recording contributes a whole-utterance embedding plus overlapping 2-second
window embeddings, so the template covers different phrase lengths. Audio is
discarded right after embedding.

Until speech recognition arrives (phase 4) the spoken words are not checked
against the phrase; the acoustic checks below still apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class Phrase:
    lang: str
    text: str


def phrases(assistant: str, owner: str) -> list[Phrase]:
    a = assistant
    return [
        Phrase("English", f"Hello {a}, authenticate me."),
        Phrase("English", "My voice is my identity. Please remember how I sound."),
        Phrase("Hindi", f"{a}, kal subah saat baje mujhe jagana."),
        Phrase("Hindi", f"Mera naam {owner} hai, aur yeh meri awaaz hai."),
        Phrase("Hinglish", f"{a}, mera calendar check kar."),
        Phrase("Hinglish", "Aaj ka weather kaisa hai, zara batao na."),
    ]


MIN_SPEECH_S = 1.2
MIN_QUALITY = 0.4
# a recording far from the earlier ones is probably someone else / noise
MIN_CONSISTENCY = 0.35


@dataclass
class VoiceEnrollmentSession:
    items: list[Phrase]
    index: int = 0
    embeddings: list[np.ndarray] = field(default_factory=list)
    per_phrase: list[np.ndarray] = field(default_factory=list)  # whole-utterance embeddings
    hint: str = ""
    last_quality: float | None = None

    @property
    def done(self) -> bool:
        return self.index >= len(self.items)

    @property
    def progress(self) -> float:
        return min(self.index / len(self.items), 1.0)

    def offer(self, windows: list[np.ndarray], speech_s: float, quality: float) -> bool:
        """Offer one recorded utterance (whole-clip embedding first). Returns accepted."""
        if self.done:
            return False
        self.last_quality = quality
        if speech_s < MIN_SPEECH_S:
            self.hint = "That was too short — read the whole phrase"
            return False
        if quality < MIN_QUALITY:
            self.hint = "Too noisy or too quiet — speak clearly, a little closer"
            return False
        whole = windows[0]
        if self.per_phrase:
            ref = np.mean(self.per_phrase, axis=0)
            ref /= max(float(np.linalg.norm(ref)), 1e-8)
            if float(ref @ whole) < MIN_CONSISTENCY:
                self.hint = "That didn't sound like your earlier recordings — try again"
                return False
        self.per_phrase.append(whole)
        self.embeddings.extend(windows)
        self.index += 1
        self.hint = ""
        return True

    def template(self) -> np.ndarray:
        return np.stack(self.embeddings).astype(np.float32)

    def snapshot(self) -> dict:
        cur = None if self.done else self.items[self.index]
        return {
            "index": self.index,
            "count": len(self.items),
            "text": cur.text if cur else "Voice enrollment complete",
            "lang": cur.lang if cur else "",
            "hint": self.hint,
            "progress": round(self.progress, 3),
            "done": self.done,
            "quality": None if self.last_quality is None else round(self.last_quality, 2),
            "phrases": [{"lang": p.lang, "text": p.text} for p in self.items],
        }
