"""Authorisation levels: what the current evidence allows right now.

  0 LOCKED   nothing (owner not verified)
  1 READ     answer questions, read calendar/notes, search files
             needs: face match + passed liveness + presence score >= t1
             (the fusion score without voice evidence: someone else talking
             nearby must not lock the owner out of reading)
  2 ACT      create things, open apps, cancel alarms
             needs: level 1 + the owner's voice verified in the last minute
             (and no unrecognised voice since) + nobody else in view
             + fusion score >= t2
  3 CONFIRM  delete notes / events: level 2 + fusion >= t3 + a liveness
             check within the last 10 minutes + an explicit confirmation
             (spoken in the verified voice, or a click)

Levels are recomputed from live evidence every time they are needed, so a
"session" lasts only as long as the evidence: the voice window expires after
a minute, face approval after 30 s without a match, liveness at random.
Hard rules come first; the fusion classifier can only lower a level.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .fusion.features import Evidence

LEVEL_NAMES = {0: "LOCKED", 1: "READ", 2: "ACT", 3: "CONFIRM"}

# why a level isn't reached (codes are mapped to spoken replies by the service)
REASONS = {
    "no_profile": "No identity profile yet",
    "scanning": "Identity not verified yet",
    "liveness": "Liveness not proven yet",
    "spoof": "Spoof suspected",
    "denied": "Unrecognised person",
    "absent": "Owner not in view",
    "low_confidence": "Combined confidence is too low",
    "bystander": "Someone else is in view",
    "voice_rejected": "An unrecognised voice spoke since your last command",
    "voice_needed": "Voice not confirmed in the last minute",
    "fresh_liveness": "A fresh liveness check is needed",
}


@dataclass
class LevelConfig:
    t1: float = 0.5
    t2: float = 0.8
    t3: float = 0.9
    voice_window_s: float = 60.0
    l3_liveness_max_age_s: float = 600.0


@dataclass
class Trust:
    level: int  # standing level 0-2 (3 is granted per action on confirmation)
    prob: float | None  # fusion score on all evidence (levels 2-3)
    presence: float | None = None  # fusion score without voice evidence (level 1)
    blockers: dict[int, str] = field(default_factory=dict)  # level -> reason code (see REASONS)
    l3_ready: bool = False
    needs_fresh_liveness: bool = False
    voice_window_s: float | None = None  # seconds left in the voice window

    @property
    def name(self) -> str:
        return LEVEL_NAMES[self.level]


def assess(ev: Evidence, prob: float | None, cfg: LevelConfig, presence: float | None = None) -> Trust:
    presence = prob if presence is None else presence
    t = Trust(0, prob, presence)
    if ev.voice_verified_age_s is not None and ev.voice_verified_age_s <= cfg.voice_window_s:
        t.voice_window_s = round(cfg.voice_window_s - ev.voice_verified_age_s, 1)

    # level 1
    if ev.face_state != "approved":
        code = ev.face_state if ev.face_state in REASONS else "scanning"
        t.blockers = {1: code, 2: code, 3: code}
        return t
    if presence is not None and presence < cfg.t1:
        t.blockers = {1: "low_confidence", 2: "low_confidence", 3: "low_confidence"}
        return t
    t.level = 1

    # level 2
    why = None
    if ev.bystander:
        why = "bystander"
    elif ev.voice_enrolled and ev.voice_rejected_since:
        why = "voice_rejected"
    elif ev.voice_enrolled and t.voice_window_s is None:
        why = "voice_needed"
    elif prob is not None and prob < cfg.t2:
        why = "low_confidence"
    if why:
        t.blockers = {2: why, 3: why}
        return t
    t.level = 2

    # level 3 (still needs an explicit confirmation per action)
    fresh = ev.liveness == "disabled" or (
        ev.liveness == "passed" and ev.live_age_s is not None and ev.live_age_s <= cfg.l3_liveness_max_age_s
    )
    if prob is not None and prob < cfg.t3:
        t.blockers = {3: "low_confidence"}
    elif not fresh:
        t.needs_fresh_liveness = True
        t.blockers = {3: "fresh_liveness"}
    else:
        t.l3_ready = True
    return t
