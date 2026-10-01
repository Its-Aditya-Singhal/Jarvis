"""Face detection + alignment + embedding via InsightFace (ONNX Runtime).

Pipeline per frame: SCRFD detector -> 5-point similarity alignment to 112x112
-> ArcFace (ResNet-50, WebFace600K) 512-d embedding. The 68-point 3D landmark
model supplies head pose (pitch/yaw/roll) and mouth/eye geometry used by the
guided enrollment; the 106-point 2D model tracks the eyelids for blink
detection, and the passive liveness model scores each face for spoofing.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

from ...hardware import profile
from ..liveness.blink import eye_openness
from ..liveness.passive import PassiveLiveness
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
        self.liveness = PassiveLiveness(self.models_root)

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
            import onnxruntime as ort
            from insightface.app import FaceAnalysis

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = profile().threads  # onnxruntime would take every core
            app = FaceAnalysis(
                name=str(self.model_dir),
                allowed_modules=["detection", "recognition", "landmark_3d_68", "landmark_2d_106"],
                sess_options=opts,
            )
            size = profile().face_det_size
            app.prepare(ctx_id=0, det_size=(size, size))
            self._app = app
            self.error = None
            if not self.liveness.load():
                log.warning("passive liveness unavailable: %s", self.liveness.error)
            return True
        except Exception as exc:  # keep the app alive if the model fails
            log.exception("face engine failed to load")
            self.error = f"face engine failed to load: {exc}"
            return False

    @property
    def ready(self) -> bool:
        return self._app is not None

    def analyze(self, frame_bgr: np.ndarray) -> list[FaceObservation]:
        import cv2  # OpenCV is only needed by the face sign-in: loaded when it runs
        if self._app is None:
            return []
        with self._lock:
            faces = self._app.get(frame_bgr)
        h, w = frame_bgr.shape[:2]
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        out: list[FaceObservation] = []
        for f in faces:
            if f.embedding is None:
                continue
            emb = np.asarray(f.normed_embedding, dtype=np.float32)
            pose = f.pose if f.pose is not None else (0.0, 0.0, 0.0)
            lmk = f.landmark_3d_68[:, :2] if f.landmark_3d_68 is not None else None
            lmk106 = getattr(f, "landmark_2d_106", None)
            kps = np.asarray(f.kps, dtype=np.float32) if f.kps is not None else None
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
                    kps=kps,
                    eye_open=eye_openness(lmk106, gray, f.bbox),
                    live_score=self.liveness.score(frame_bgr, kps),
                )
            )
        # largest face first
        out.sort(key=lambda o: o.width, reverse=True)
        return out
