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

    @property
    def width(self) -> float:
        return float(self.bbox[2] - self.bbox[0])

    @property
    def rel_width(self) -> float:
        return self.width / max(self.frame_width, 1)
