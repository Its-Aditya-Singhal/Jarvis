"""Fusion classifier: logistic regression over the evidence features and
their pairwise products (degree-2 expansion), which lets a linear model
express rules like "a high face score only counts with a live face" or "a
voice result only helps if it matches".

Inference is a dot product and a sigmoid (NumPy only), so it runs on every
frame for free. The model is a small JSON file: weights, the standardisation
used in training, and the evaluation report shown in the app.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .features import FEATURES

DEFAULT_PATH = Path(__file__).with_name("default_model.json")
_PAIRS = [(i, j) for i in range(len(FEATURES)) for j in range(i, len(FEATURES))]


def expand(x: np.ndarray) -> np.ndarray:
    """Base features followed by all pairwise products (including squares)."""
    x = np.atleast_2d(np.asarray(x, dtype=np.float64))
    return np.hstack([x, np.stack([x[:, i] * x[:, j] for i, j in _PAIRS], axis=1)])


@dataclass
class FusionModel:
    mean: np.ndarray
    scale: np.ndarray
    coef: np.ndarray
    intercept: float
    meta: dict = field(default_factory=dict)

    def _z(self, x: np.ndarray) -> np.ndarray:
        return (expand(x) - self.mean) / self.scale

    def prob(self, x: np.ndarray) -> np.ndarray | float:
        """P(the live owner is in control) for one vector (float) or a batch."""
        single = np.ndim(x) == 1
        logit = self._z(x) @ self.coef + self.intercept
        p = 1.0 / (1.0 + np.exp(-np.clip(logit, -40, 40)))
        return float(p[0]) if single else p

    # -- persistence -----------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "type": "logistic_regression_poly2",
            "features": FEATURES,
            "mean": self.mean.round(6).tolist(),
            "scale": self.scale.round(6).tolist(),
            "coef": self.coef.round(6).tolist(),
            "intercept": round(float(self.intercept), 6),
            **self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FusionModel":
        if d.get("type") != "logistic_regression_poly2" or d.get("features") != FEATURES:
            raise ValueError("incompatible fusion model")
        meta = {k: v for k, v in d.items() if k not in ("type", "features", "mean", "scale", "coef", "intercept")}
        return cls(np.array(d["mean"]), np.array(d["scale"]), np.array(d["coef"]), float(d["intercept"]), meta)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=1))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> "FusionModel":
        return cls.from_dict(json.loads(path.read_text()))


def load_model(personal: Path | None) -> tuple[FusionModel | None, str]:
    """The owner's retrained model if present, else the shipped one.
    Returns (model or None, source: personal | default | error text)."""
    if personal is not None and personal.exists():
        try:
            return FusionModel.load(personal), "personal"
        except (ValueError, KeyError, json.JSONDecodeError):
            pass  # fall back to the shipped model
    try:
        return FusionModel.load(DEFAULT_PATH), "default"
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return None, f"fusion model unavailable: {exc}"
