"""Guided voice enrollment (training side).

The owner reads 24 short phrases in English, Hindi and Hinglish, in their normal voice, softly,
louder and from a step back. Each accepted
recording contributes a whole-utterance embedding plus overlapping 2-second
window embeddings, so the template covers different phrase lengths. Audio is
discarded right after embedding.

The spoken words are checked against the displayed phrase with local speech
recognition (see ``SpeechService.check_phrase``) before the acoustic checks
below, so a stray recording or unrelated speech isn't enrolled.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class Phrase:
    lang: str
    text: str
    how: str = ""  # how to say it (softly, from a step back…): the voiceprint should cover how you really talk


NORMAL = "In your normal voice"
SOFT = "Softly, as if someone nearby is asleep"
AWAY = "From a step back from the Mac"
LOUD = "A little louder, as if across the room"
QUICK = "Quickly and casually, the way you'd really ask"


def phrases(assistant: str, owner: str) -> list[Phrase]:
    """24 phrases: a long enrollment makes a voiceprint that keeps working across moods, volumes and
    distances, so the strict threshold can refuse other voices without refusing the owner."""
    a, o = assistant, owner or "boss"
    return [
        Phrase("English", f"Hello {a}, authenticate me.", NORMAL),
        Phrase("English", "My voice is my identity. Please remember how I sound.", NORMAL),
        Phrase("Hindi", f"{a}, kal subah saat baje mujhe jagana.", NORMAL),
        Phrase("Hindi", f"Mera naam {o} hai, aur yeh meri awaaz hai.", NORMAL),
        Phrase("Hinglish", f"{a}, mera calendar check kar.", NORMAL),
        Phrase("Hinglish", "Aaj ka weather kaisa hai, zara batao na.", NORMAL),
        Phrase("English", f"{a}, summarise my last ten emails.", NORMAL),
        Phrase("English", f"{a}, what's on my calendar tomorrow morning?", NORMAL),
        Phrase("English", "Send Rahul a mail saying I'll be there by seven.", NORMAL),
        Phrase("English", "Find the project plan in my Google Drive and read it to me.", NORMAL),
        Phrase("Hindi", "Aaj ke saare naye emails ka summary batao.", NORMAL),
        Phrase("Hindi", "Mere calendar mein kal shaam paanch baje meeting daal do.", NORMAL),
        Phrase("Hinglish", f"{a}, Rahul ko mail bhejo ki main aa raha hoon.", NORMAL),
        Phrase("English", f"{a}, read my latest email.", SOFT),
        Phrase("Hinglish", f"{a}, screen ki brightness thodi kam karo.", SOFT),
        Phrase("English", f"{a}, open Safari and play some music.", AWAY),
        Phrase("Hindi", f"{a}, abhi kitne baje hain?", AWAY),
        Phrase("English", f"{a}, take a screenshot and save it to the desktop.", LOUD),
        Phrase("Hinglish", f"{a}, das minute ka timer laga do.", LOUD),
        Phrase("English", f"{a}, any new mail for me today?", QUICK),
        Phrase("English", f"Okay {a}, yes, go ahead and send it.", QUICK),
        Phrase("Hinglish", f"{a}, volume thoda badhao aur gaana chalao.", QUICK),
        Phrase("English", f"{a}. Hey {a}. Are you there, {a}?", "The way you'd call me"),
        Phrase("English", f"Thank you {a}, that's all for now.", NORMAL),
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
            "how": cur.how if cur else "",
            "hint": self.hint,
            "progress": round(self.progress, 3),
            "done": self.done,
            "quality": None if self.last_quality is None else round(self.last_quality, 2),
            "phrases": [{"lang": p.lang, "text": p.text} for p in self.items],
        }
