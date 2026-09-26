"""Face image quality scoring (0..1). Pure functions, no model calls."""

from __future__ import annotations

import cv2
import numpy as np


def _ramp(x: float, lo: float, hi: float) -> float:
    return float(np.clip((x - lo) / (hi - lo), 0.0, 1.0))


def sharpness(gray_crop: np.ndarray) -> float:
    """Variance of the Laplacian: low for blurred / out-of-focus crops."""
    if gray_crop.size == 0:
        return 0.0
    return float(cv2.Laplacian(gray_crop, cv2.CV_64F).var())


def face_crop(frame: np.ndarray, bbox: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = (int(v) for v in bbox)
    x1, y1 = max(x1, 0), max(y1, 0)
    x2, y2 = min(x2, w), min(y2, h)
    return frame[y1:y2, x1:x2]


def quality_score(det_score: float, face_width_px: float, sharp: float, brightness: float) -> float:
    """Combine detector confidence, size, focus and exposure into one score."""
    size_q = _ramp(face_width_px, 50, 130)
    sharp_q = _ramp(sharp, 15, 120)
    # penalise very dark or blown-out crops (mean grey level 0..255)
    exposure_q = 1.0 - min(abs(brightness - 125.0) / 110.0, 1.0)
    det_q = _ramp(det_score, 0.5, 0.85)
    # geometric mean so a single very bad factor dominates
    parts = np.array([size_q, sharp_q, exposure_q, det_q]) + 1e-6
    return float(np.exp(np.log(parts).mean()))


def frame_quality(frame: np.ndarray, bbox: np.ndarray, det_score: float) -> float:
    crop = face_crop(frame, bbox)
    if crop.size == 0:
        return 0.0
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return quality_score(det_score, float(bbox[2] - bbox[0]), sharpness(gray), float(gray.mean()))
