"""Evidence gathered from the face, liveness and voice pipelines, as a feature vector.

Only scores and ages are used, never embeddings or raw media, so a stored
feature vector reveals nothing about what the owner looks or sounds like.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

FEATURES = [
    "face_sim",       # smoothed cosine similarity of the best face to the owner template
    "face_fresh",     # 1 = matched just now, 0 = last match a full re-auth interval ago
    "face_quality",   # detector confidence x size x sharpness x frontal-ness
    "live_score",     # passive anti-spoof median (0.5 when the model is unavailable)
    "live_fresh",     # 1 = liveness challenge just passed, 0 = not passed / long ago
    "voice_present",  # a speaker-verification result exists in the voice window
    "voice_sim",      # its cosine similarity (0 when absent)
    "voice_fresh",    # 1 = just spoke, 0 = window expired
    "others",         # other faces in view (0, 0.5, 1 for two or more)
]

# normalisation horizons (seconds)
FACE_FRESH_S = 30.0
LIVE_FRESH_S = 900.0
VOICE_WINDOW_S = 60.0


@dataclass
class Evidence:
    face_state: str = "scanning"   # effective state: approved | liveness | spoof | denied | absent | scanning | no_profile
    liveness: str = "idle"         # gate state, or "disabled"
    face_sim: float | None = None
    face_age_s: float | None = None
    face_quality: float | None = None
    live_score: float | None = None
    live_age_s: float | None = None     # since the last passed challenge
    voice_sim: float | None = None      # last speaker-verification result in the window
    voice_age_s: float | None = None
    voice_verified_age_s: float | None = None
    voice_rejected_since: bool = False  # an unrecognised voice spoke after the last owner match
    voice_enrolled: bool = True
    others: int = 0
    bystander: bool = False


def _fresh(age: float | None, horizon: float) -> float:
    if age is None:
        return 0.0
    return float(np.clip(1.0 - age / horizon, 0.0, 1.0))


VOICE_FEATURES = ("voice_present", "voice_sim", "voice_fresh")


def without_voice(x: np.ndarray) -> np.ndarray:
    """The same evidence as if nobody had spoken (for the presence score)."""
    x = np.array(x, dtype=np.float64)
    for k in VOICE_FEATURES:
        x[..., FEATURES.index(k)] = 0.0
    return x


def vector(ev: Evidence, voice_window_s: float = VOICE_WINDOW_S) -> np.ndarray:
    voice = ev.voice_sim is not None and ev.voice_age_s is not None and ev.voice_age_s <= voice_window_s
    return np.array(
        [
            ev.face_sim if ev.face_sim is not None else 0.0,
            _fresh(ev.face_age_s, FACE_FRESH_S),
            ev.face_quality if ev.face_quality is not None else 0.0,
            ev.live_score if ev.live_score is not None else 0.5,
            _fresh(ev.live_age_s, LIVE_FRESH_S) if ev.liveness == "passed" else 0.5 if ev.liveness == "disabled" else 0.0,
            1.0 if voice else 0.0,
            ev.voice_sim if voice else 0.0,
            _fresh(ev.voice_age_s, voice_window_s) if voice else 0.0,
            min(ev.others, 2) / 2.0,
        ],
        dtype=np.float64,
    )
