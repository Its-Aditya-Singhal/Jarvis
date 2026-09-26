"""Face detection + alignment + embedding via InsightFace (ONNX Runtime).

Pipeline per frame: SCRFD detector -> 5-point similarity alignment to 112x112
-> ArcFace (ResNet-50, WebFace600K) 512-d embedding. The 68-point 3D landmark
model supplies head pose (pitch/yaw/roll) and mouth/eye geometry used by the
guided enrollment.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

from .quality import frame_quality
from .types import FaceObservation

log = logging.getLogger(__name__)


class FaceEngine:
    def __init__(self, models_root: Path, pack: str = "buffalo_l"):
        self.models_root = Path(models_root)
        self.pack = pack
        self._app = None
        self._lock = threading.Lock()
        self.error: str | None = None

    @property
    def model_dir(self) -> Path:
        return self.models_root / "models" / self.pack

    @property
    def available(self) -> bool:
        return self.model_dir.is_dir()

    def load(self) -> bool:
        """Load models from disk. Never downloads: see scripts/download_models.py."""
        if self._app is not None:
            return True
        if not self.available:
            self.error = f"face models missing at {self.model_dir}"
            return False
        try:
            from insightface.app import FaceAnalysis

            app = FaceAnalysis(
                name=str(self.model_dir),
                allowed_modules=["detection", "recognition", "landmark_3d_68"],
            )
            app.prepare(ctx_id=0, det_size=(640, 640))
            self._app = app
            self.error = None
            return True
        except Exception as exc:  # keep the app alive if the model fails
            log.exception("face engine failed to load")
            self.error = f"face engine failed to load: {exc}"
            return False

    @property
    def ready(self) -> bool:
        return self._app is not None

    def analyze(self, frame_bgr: np.ndarray) -> list[FaceObservation]:
        if self._app is None:
            return []
        with self._lock:
            faces = self._app.get(frame_bgr)
        h, w = frame_bgr.shape[:2]
        out: list[FaceObservation] = []
        for f in faces:
            if f.embedding is None:
                continue
            emb = np.asarray(f.normed_embedding, dtype=np.float32)
            pose = f.pose if f.pose is not None else (0.0, 0.0, 0.0)
            lmk = f.landmark_3d_68[:, :2] if f.landmark_3d_68 is not None else None
            out.append(
                FaceObservation(
                    bbox=np.asarray(f.bbox, dtype=np.float32),
                    det_score=float(f.det_score),
                    embedding=emb,
                    pitch=float(pose[0]),
                    yaw=float(pose[1]),
                    roll=float(pose[2]),
                    landmarks=lmk,
                    quality=frame_quality(frame_bgr, f.bbox, float(f.det_score)),
                    frame_width=w,
                    frame_height=h,
                )
            )
        # largest face first
        out.sort(key=lambda o: o.width, reverse=True)
        return out
