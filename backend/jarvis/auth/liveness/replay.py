"""Replay heuristics that don't need a model.

A physical camera sensor never produces two bit-identical frames: there is
always noise. Identical consecutive frames therefore mean a synthetic source
(a virtual camera playing a still image or a paused video) or a feed that has
stopped updating; neither may count as evidence of a live person.
"""

from __future__ import annotations

import hashlib

import numpy as np


class FrozenFeedDetector:
    def __init__(self, frozen_after_s: float = 2.0):
        self.frozen_after_s = frozen_after_s
        self._sig: bytes | None = None
        self._since: float | None = None

    def update(self, frame: np.ndarray, now: float) -> bool:
        """Returns True while the feed has been frozen for ``frozen_after_s``."""
        # raw strided pixels (not a resize, which would average the noise away)
        sig = hashlib.blake2b(np.ascontiguousarray(frame[::7, ::7]).tobytes(), digest_size=16).digest()
        if sig != self._sig:
            self._sig = sig
            self._since = None
            return False
        if self._since is None:
            self._since = now
        return now - self._since >= self.frozen_after_s
