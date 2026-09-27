"""Blink detection from the 106-point landmark eye contours.

Eye openness combines two cues, averaged over both eyes:
  * geometry — contour height / width (the lids come together), and
  * appearance — pixel contrast inside the contour relative to the face's
    brightness (an open eye shows white sclera against a dark iris; a closed
    lid is flat skin). Landmark models respond weakly to closed eyes on small
    faces, and the contrast cue keeps blinks detectable there.
Absolute values vary a lot between people and lighting, so a blink is
detected relative to a rolling baseline of the person's own open-eye value: a
drop below ``close_ratio`` x baseline and recovery within ``max_closed_s``.
"""

from __future__ import annotations

from collections import deque

import cv2
import numpy as np

# eye contour indices in InsightFace's 2d106det layout
_EYES = (np.arange(33, 43), np.arange(87, 97))


def eye_openness(
    landmarks_106: np.ndarray | None,
    gray: np.ndarray | None = None,
    face_bbox: np.ndarray | None = None,
) -> float | None:
    """Openness index (unitless, only meaningful relative to the same person).

    With ``gray`` (the frame in grayscale) and ``face_bbox`` the appearance cue
    is included; without them only the contour geometry is used.
    """
    if landmarks_106 is None or len(landmarks_106) < 106:
        return None
    ref = None
    if gray is not None and face_bbox is not None:
        h, w = gray.shape[:2]
        x1, y1, x2, y2 = (int(v) for v in face_bbox)
        crop = gray[max(y1, 0) : min(y2, h), max(x1, 0) : min(x2, w)]
        if crop.size:
            ref = max(float(np.median(crop)), 1.0)
    values = []
    for idx in _EYES:
        e = landmarks_106[idx]
        width = float(e[:, 0].max() - e[:, 0].min())
        if width < 1e-6:
            return None
        value = float(e[:, 1].max() - e[:, 1].min()) / width
        if ref is not None and gray is not None:
            mask = np.zeros(gray.shape[:2], np.uint8)
            cv2.fillPoly(mask, [cv2.convexHull(e.astype(np.int32))], 1)
            px = gray[mask > 0]
            value *= float(px.std()) / ref if px.size >= 6 else 0.0
        values.append(value)
    return float(np.mean(values))


class BlinkDetector:
    def __init__(
        self,
        close_ratio: float = 0.75,
        reopen_ratio: float = 0.9,
        max_closed_s: float = 0.9,
        history: int = 40,
        min_history: int = 5,
    ):
        self.close_ratio = close_ratio
        self.reopen_ratio = reopen_ratio
        self.max_closed_s = max_closed_s
        self.min_history = min_history
        self._open: deque[float] = deque(maxlen=history)
        self._closed_t: float | None = None

    @property
    def baseline(self) -> float | None:
        if len(self._open) < self.min_history:
            return None
        return float(np.percentile(self._open, 70))

    def reset(self) -> None:
        self._open.clear()
        self._closed_t = None

    def update(self, openness: float | None, now: float) -> bool:
        """Feed one frame. Returns True on the frame a blink completes."""
        if openness is None:
            return False
        base = self.baseline
        if base is None:
            self._open.append(openness)
            return False
        if self._closed_t is None:
            if openness < base * self.close_ratio:
                self._closed_t = now
            else:
                self._open.append(openness)
            return False
        if openness > base * self.reopen_ratio:
            blink = now - self._closed_t <= self.max_closed_s
            self._closed_t = None
            self._open.append(openness)
            return blink
        if now - self._closed_t > 3 * self.max_closed_s:
            # eyes "closed" far too long: looking down or tracking drift; re-learn
            self.reset()
        return False
