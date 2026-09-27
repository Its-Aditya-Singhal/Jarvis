"""Passive (single-frame) face anti-spoofing.

Uses InsightFace's liveness addon: an 80x80 RGB CNN on a five-point aligned
crop that outputs a live probability. Printed photos, phone/laptop screens and
low-resolution re-captures score low because their texture, moire and colour
response differ from a face seen directly by the camera sensor.

The model file (``models/addons/liveness.onnx``, 1.5 MB, SHA-256 verified) is
fetched by ``scripts/download_models.py``; it is never downloaded at runtime.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

MODEL_FILE = "liveness.onnx"


class PassiveLiveness:
    def __init__(self, models_root: Path):
        self.path = Path(models_root) / "addons" / MODEL_FILE
        self._model = None
        self.error: str | None = None

    @property
    def ready(self) -> bool:
        return self._model is not None

    def load(self) -> bool:
        if self._model is not None:
            return True
        if not self.path.is_file():
            self.error = f"liveness model missing at {self.path}"
            return False
        try:
            from insightface.addons.catalog import ADDON_CATALOG, _verify
            from insightface.addons.liveness import Liveness

            _verify(self.path, ADDON_CATALOG["liveness"])
            self._model = Liveness(self.path, providers=["CPUExecutionProvider"])
            self.error = None
            return True
        except Exception as exc:
            log.exception("liveness model failed to load")
            self.error = f"liveness model failed to load: {exc}"
            return False

    def score(self, frame_bgr: np.ndarray, kps: np.ndarray | None) -> float | None:
        """Live probability in [0, 1], or None when it can't be judged
        (model not loaded, no keypoints, or the face is cut off by the frame edge)."""
        if self._model is None or kps is None:
            return None
        try:
            result = self._model.predict(frame_bgr, kps)
        except Exception:
            log.debug("liveness prediction failed", exc_info=True)
            return None
        return result["live_score"] if result["status"] == "ok" else None
