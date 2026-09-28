"""Owner preferences changed at runtime from the Settings page.

Every preference is a choice from a fixed list, never a free number, so no
setting can be pushed to an unsafe value. Security preferences list their
choices from the loosest to the strictest: moving towards the start of the
list widens access and needs a level-3 confirmation; tightening needs only
level 2. Values live in the profile table as ``pref.<key>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .database.db import Database


@dataclass(frozen=True)
class Pref:
    key: str
    choices: tuple[Any, ...]
    default: Any
    security: bool = False  # choices ordered loosest -> strictest
    labels: tuple[str, ...] = ()


PREFS: dict[str, Pref] = {p.key: p for p in [
    # voice
    Pref("voice.speed", (0.9, 1.0, 1.1, 1.2), 1.0, labels=("Calm", "Normal", "Brisk", "Fast")),
    Pref("voice.followup_s", (5.0, 8.0, 12.0), 8.0, labels=("5 s", "8 s", "12 s")),
    Pref("voice.ack", ("ping", "say"), "ping", labels=("Beep", "Say “Yes?”")),
    # security (loosest -> strictest)
    Pref("security.face", ("standard", "strict"), "standard", True, ("Standard", "Strict")),
    Pref("security.voice", ("standard", "strict"), "standard", True, ("Standard", "Strict")),
    Pref("security.away_lock_s", (60.0, 20.0, 8.0), 8.0, True, ("After 1 min", "After 20 s", "After 8 s")),
    Pref("security.typed", ("on", "off"), "on", True, ("Voice or typed", "Voice only")),
    Pref("security.camera", ("once", "always"), "once", True, ("Face once, then voice", "Face all the time")),
    Pref("security.liveness", ("normal", "frequent"), "normal", True, ("Every 5-15 min", "Every 2-5 min")),
    # any command (generated AppleScript): what runs without asking (8 GB Macs always ask)
    Pref("security.scripts", ("changes", "always", "off"), "changes", True,
         ("Ask before changes", "Always ask", "Off")),
    # memory
    Pref("memory.enabled", (False, True), True, labels=("Paused", "On")),
    Pref("memory.suggestions", (False, True), True, labels=("Off", "On")),
    # performance & privacy
    Pref("perf.mode", ("auto", "fast", "balanced", "quality"), "auto",
         labels=("Auto (Fast on battery)", "Fast", "Balanced", "Quality")),
    Pref("privacy.offline", (False, True), True, True, ("Allowed", "Blocked")),
]}

# what the security presets mean
FACE_PRESETS = {"standard": (0.42, 0.25), "strict": (0.50, 0.30)}  # (accept, reject) cosine
VOICE_PRESETS = {"standard": (0.50, 0.30), "strict": (0.58, 0.35)}
LIVENESS_PRESETS = {"normal": (300.0, 900.0), "frequent": (120.0, 300.0)}  # random re-check window


def coerce(pref: Pref, value: Any) -> Any:
    """The matching choice for ``value`` (JSON may turn 8.0 into 8), or ValueError."""
    for c in pref.choices:
        if isinstance(c, bool) or isinstance(value, bool):
            if isinstance(c, bool) and isinstance(value, bool) and c == value:
                return c
        elif c == value:
            return c
    raise ValueError(f"{pref.key} must be one of {', '.join(map(str, pref.choices))}")


class Prefs:
    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str) -> Any:
        p = PREFS[key]
        raw = self.db.get(f"pref.{key}")
        if raw is None:
            return p.default
        for c in p.choices:
            if str(c) == raw:
                return c
        return p.default  # an old or tampered value falls back to the default

    def set(self, key: str, value: Any) -> Any:
        p = PREFS[key]
        v = coerce(p, value)
        self.db.set(f"pref.{key}", str(v))
        return v

    def loosens(self, key: str, value: Any) -> bool:
        """Would this change widen access? (needs level 3)"""
        p = PREFS[key]
        if not p.security:
            return False
        return p.choices.index(coerce(p, value)) < p.choices.index(self.get(key))

    def all(self) -> dict[str, Any]:
        return {k: self.get(k) for k in PREFS}

    def describe(self) -> list[dict]:
        return [
            {"key": p.key, "value": self.get(p.key), "choices": list(p.choices),
             "labels": list(p.labels or map(str, p.choices)), "security": p.security}
            for p in PREFS.values()
        ]
