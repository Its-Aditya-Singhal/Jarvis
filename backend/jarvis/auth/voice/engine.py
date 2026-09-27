"""Speaker embeddings with ECAPA-TDNN (SpeechBrain, trained on VoxCeleb 1+2).

Pipeline per utterance: 80-bin log-Mel filterbank -> per-utterance mean
normalisation -> ECAPA-TDNN (SE-Res2Net blocks, attentive statistics
pooling) -> 192-d speaker embedding, L2-normalised. Only the embedding network
is loaded; the VoxCeleb classification head isn't needed for verification.
Weights are read from ``models/speechbrain/spkrec-ecapa-voxceleb`` and never
downloaded at runtime.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SR = 16000


class SpeakerEngine:
    def __init__(self, models_root: Path):
        self.model_dir = Path(models_root) / "speechbrain" / "spkrec-ecapa-voxceleb"
        self.error: str | None = None
        self._lock = threading.Lock()
        self._modules = None

    @property
    def available(self) -> bool:
        return (self.model_dir / "embedding_model.ckpt").exists()

    @property
    def ready(self) -> bool:
        return self._modules is not None

    def load(self) -> bool:
        if self._modules is not None:
            return True
        if not self.available:
            self.error = f"voice model missing at {self.model_dir}"
            return False
        try:
            import torch
            from speechbrain.lobes.features import Fbank
            from speechbrain.lobes.models.ECAPA_TDNN import ECAPA_TDNN
            from speechbrain.processing.features import InputNormalization

            torch.set_num_threads(2)
            fbank = Fbank(n_mels=80)
            norm = InputNormalization(norm_type="sentence", std_norm=False)
            model = ECAPA_TDNN(
                input_size=80,
                channels=[1024, 1024, 1024, 1024, 3072],
                kernel_sizes=[5, 3, 3, 3, 1],
                dilations=[1, 2, 3, 4, 1],
                attention_channels=128,
                lin_neurons=192,
            )
            state = torch.load(self.model_dir / "embedding_model.ckpt", map_location="cpu", weights_only=True)
            model.load_state_dict(state)
            model.eval()
            self._modules = (torch, fbank, norm, model)
            self.error = None
            return True
        except Exception as exc:
            log.exception("speaker engine failed to load")
            self.error = f"voice engine failed to load: {exc}"
            return False

    def embed(self, audio: np.ndarray) -> np.ndarray:
        """192-d L2-normalised speaker embedding of 16 kHz mono float32 audio."""
        if self._modules is None:
            raise RuntimeError("speaker engine not loaded")
        torch, fbank, norm, model = self._modules
        with self._lock, torch.inference_mode():
            wav = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))[None]
            lens = torch.ones(1)
            feats = norm(fbank(wav), lens)
            emb = model(feats, lens).squeeze().numpy().astype(np.float32)
        return emb / max(float(np.linalg.norm(emb)), 1e-8)

    def embed_windows(self, audio: np.ndarray, win_s: float = 2.0, hop_s: float = 1.0) -> list[np.ndarray]:
        """Embeddings of the whole clip plus overlapping windows (enrollment augmentation)."""
        out = [self.embed(audio)]
        win, hop = int(win_s * SR), int(hop_s * SR)
        if len(audio) >= win + hop:
            for start in range(0, len(audio) - win + 1, hop):
                out.append(self.embed(audio[start : start + win]))
        return out
