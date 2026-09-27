from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class FaceObservation:
    """One detected face in one frame. Lives only in memory."""

    bbox: np.ndarray  # x1, y1, x2, y2 in pixels
    det_score: float
    embedding: np.ndarray  # L2-normalised 512-d ArcFace vector
    pitch: float
    yaw: float
    roll: float
    landmarks: np.ndarray | None  # 68 x 2, pixels
    quality: float
    frame_width: int
    frame_height: int
    kps: np.ndarray | None = None  # 5 detector keypoints, pixels
    eye_open: float | None = None  # eye openness from the 106-point landmarks
    live_score: float | None = None  # passive anti-spoof live probability

    @property
    def width(self) -> float:
        return float(self.bbox[2] - self.bbox[0])

    @property
    def rel_width(self) -> float:
        return self.width / max(self.frame_width, 1)

    @property
    def center(self) -> tuple[float, float]:
        return (
            float(self.bbox[0] + self.bbox[2]) / 2 / max(self.frame_width, 1),
            float(self.bbox[1] + self.bbox[3]) / 2 / max(self.frame_height, 1),
        )
