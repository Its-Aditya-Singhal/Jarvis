"""Simulated evidence for training the fusion classifier.

There is no public dataset of "face score + voice score + liveness" from the
same sessions, so the base model is trained on simulated scenarios whose
score distributions follow what the pipelines produce on this machine
(owner face similarity ~0.6 against the 0.42 threshold, strangers ~0.1,
owner voice ~0.6 against 0.50, passive anti-spoof ~0.8 live vs ~0.2 for
photos). Real samples collected on the device (see ``samples.py``) are mixed
in when the owner retrains, so the model adapts to their actual scores.

Label 1 = the live, verified owner is the one in control; 0 = anyone or
anything else (stranger, look-alike, photo, replayed video, someone else
speaking while the owner sits at the screen, stale evidence).
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from .features import FEATURES

Scenario = Callable[[np.random.Generator, int], np.ndarray]


def _n(rng, mu, sd, n, lo=-0.2, hi=1.0):
    return np.clip(rng.normal(mu, sd, n), lo, hi)


def _maybe(rng, p, n):
    return rng.random(n) < p


def _build(n, **cols) -> np.ndarray:
    out = np.zeros((n, len(FEATURES)))
    for k, v in cols.items():
        out[:, FEATURES.index(k)] = v
    return out


def _voice(rng, n, p, mu, sd, fresh=(0.0, 1.0)):
    present = _maybe(rng, p, n)
    return {
        "voice_present": present.astype(float),
        "voice_sim": np.where(present, _n(rng, mu, sd, n), 0.0),
        "voice_fresh": np.where(present, rng.uniform(*fresh, n), 0.0),
    }


def _live_score(rng, n, a, b, missing=0.1):
    s = rng.beta(a, b, n)
    return np.where(_maybe(rng, missing, n), 0.5, s)  # passive model unavailable


def _others(rng, n, p):
    return np.where(_maybe(rng, p, n), 0.5, 0.0)


# -- genuine owner --------------------------------------------------------------------
def owner_speaking(rng, n):
    return _build(n, face_sim=_n(rng, 0.62, 0.09, n), face_fresh=rng.uniform(0.6, 1, n),
                  face_quality=rng.uniform(0.45, 0.95, n), live_score=_live_score(rng, n, 9, 2),
                  live_fresh=rng.uniform(0.05, 1, n), others=_others(rng, n, 0.1),
                  **_voice(rng, n, 1.0, 0.62, 0.09, (0.2, 1)))


def owner_silent(rng, n):
    return _build(n, face_sim=_n(rng, 0.62, 0.09, n), face_fresh=rng.uniform(0.6, 1, n),
                  face_quality=rng.uniform(0.45, 0.95, n), live_score=_live_score(rng, n, 9, 2),
                  live_fresh=rng.uniform(0.05, 1, n), others=_others(rng, n, 0.1),
                  **_voice(rng, n, 0.0, 0, 0))


def owner_hard_conditions(rng, n):
    """Dim light, glasses, noisy room: weaker scores, still the owner."""
    return _build(n, face_sim=_n(rng, 0.5, 0.07, n), face_fresh=rng.uniform(0.3, 1, n),
                  face_quality=rng.uniform(0.35, 0.6, n), live_score=_live_score(rng, n, 5, 3),
                  live_fresh=rng.uniform(0.0, 1, n), others=_others(rng, n, 0.1),
                  **_voice(rng, n, 0.6, 0.5, 0.1))


# -- everything else --------------------------------------------------------------------
def stranger(rng, n):
    return _build(n, face_sim=_n(rng, 0.1, 0.08, n, hi=0.4), face_fresh=np.where(_maybe(rng, 0.8, n), 0, rng.uniform(0, 0.3, n)),
                  face_quality=rng.uniform(0.4, 0.95, n), live_score=_live_score(rng, n, 9, 2),
                  live_fresh=0.0, others=_others(rng, n, 0.3), **_voice(rng, n, 0.5, 0.15, 0.1))


def lookalike(rng, n):
    """Sibling or similar-looking person: borderline face, wrong voice."""
    return _build(n, face_sim=_n(rng, 0.36, 0.06, n), face_fresh=rng.uniform(0, 0.5, n),
                  face_quality=rng.uniform(0.4, 0.95, n), live_score=_live_score(rng, n, 9, 2),
                  live_fresh=np.where(_maybe(rng, 0.7, n), 0, rng.uniform(0, 1, n)),
                  others=_others(rng, n, 0.2), **_voice(rng, n, 0.6, 0.25, 0.1))


def photo_or_screen(rng, n):
    """The owner's photo held up (maybe with a recording of their voice)."""
    return _build(n, face_sim=_n(rng, 0.6, 0.1, n), face_fresh=rng.uniform(0.5, 1, n),
                  face_quality=rng.uniform(0.3, 0.8, n), live_score=_live_score(rng, n, 2, 7, 0.0),
                  live_fresh=np.where(_maybe(rng, 0.9, n), 0, rng.uniform(0, 0.3, n)),
                  others=0.0, **_voice(rng, n, 0.4, 0.5, 0.12))


def video_replay(rng, n):
    return _build(n, face_sim=_n(rng, 0.58, 0.1, n), face_fresh=rng.uniform(0.5, 1, n),
                  face_quality=rng.uniform(0.3, 0.8, n), live_score=_live_score(rng, n, 4, 4, 0.0),
                  live_fresh=np.where(_maybe(rng, 0.8, n), 0, rng.uniform(0, 0.5, n)),
                  others=0.0, **_voice(rng, n, 0.7, 0.5, 0.12))


def someone_else_speaks(rng, n):
    """The owner is at the screen but another person gives the command."""
    return _build(n, face_sim=_n(rng, 0.62, 0.09, n), face_fresh=rng.uniform(0.6, 1, n),
                  face_quality=rng.uniform(0.45, 0.95, n), live_score=_live_score(rng, n, 9, 2),
                  live_fresh=rng.uniform(0.05, 1, n), others=_others(rng, n, 0.5),
                  **_voice(rng, n, 1.0, 0.15, 0.1, (0.5, 1)))


def stale(rng, n):
    """Old evidence only: nobody has been matched recently."""
    return _build(n, face_sim=_n(rng, 0.5, 0.15, n), face_fresh=0.0, face_quality=rng.uniform(0.3, 0.9, n),
                  live_score=_live_score(rng, n, 6, 3), live_fresh=0.0, others=0.0, **_voice(rng, n, 0.0, 0, 0))


SCENARIOS: dict[str, tuple[Scenario, int, float]] = {
    # name: (generator, label, share of the dataset)
    "owner_speaking": (owner_speaking, 1, 0.30),
    "owner_silent": (owner_silent, 1, 0.25),
    "owner_hard_conditions": (owner_hard_conditions, 1, 0.15),
    "stranger": (stranger, 0, 0.08),
    "lookalike": (lookalike, 0, 0.05),
    "photo_or_screen": (photo_or_screen, 0, 0.06),
    "video_replay": (video_replay, 0, 0.04),
    "someone_else_speaks": (someone_else_speaks, 0, 0.05),
    "stale": (stale, 0, 0.02),
}


def generate(n: int = 20000, seed: int = 7) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns X (n, features), y (n,), and the scenario name of each row."""
    rng = np.random.default_rng(seed)
    xs, ys, names = [], [], []
    for name, (fn, label, share) in SCENARIOS.items():
        k = max(1, int(round(n * share)))
        xs.append(fn(rng, k))
        ys.append(np.full(k, label))
        names.append(np.full(k, name))
    X, y, s = np.vstack(xs), np.concatenate(ys), np.concatenate(names)
    order = rng.permutation(len(y))
    return X[order], y[order].astype(float), s[order]
