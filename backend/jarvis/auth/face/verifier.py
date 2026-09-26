"""Face verification against the enrolled template (inference side)."""

from __future__ import annotations

import math

import numpy as np


class FaceVerifier:
    def __init__(self, template: np.ndarray, top_k: int = 5):
        t = np.asarray(template, dtype=np.float32)
        norms = np.linalg.norm(t, axis=1, keepdims=True)
        self.template = t / np.maximum(norms, 1e-8)
        self.top_k = max(1, min(top_k, len(self.template)))

    def similarity(self, embedding: np.ndarray) -> float:
        """Mean of the top-k cosine similarities to the enrolled embeddings.

        Top-k (rather than max) makes a single lucky match less influential,
        and (rather than the centroid) keeps pose-specific samples useful.
        """
        e = np.asarray(embedding, dtype=np.float32)
        e = e / max(float(np.linalg.norm(e)), 1e-8)
        sims = self.template @ e
        k = self.top_k
        return float(np.sort(sims)[-k:].mean())

    def self_consistency(self) -> float:
        """Leave-one-out similarity of enrollment samples (template health)."""
        n = len(self.template)
        if n < 2:
            return 1.0
        sims = self.template @ self.template.T
        np.fill_diagonal(sims, -1.0)
        k = min(self.top_k, n - 1)
        return float(np.sort(sims, axis=1)[:, -k:].mean())


def confidence(similarity: float, threshold: float, steepness: float = 14.0) -> float:
    """Map a cosine similarity to a 0..1 confidence centred on the threshold."""
    return 1.0 / (1.0 + math.exp(-(similarity - threshold) * steepness))
