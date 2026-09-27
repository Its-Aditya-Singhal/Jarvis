"""Training and evaluation of the fusion classifier (NumPy only).

L2-regularised logistic regression on the degree-2 feature expansion,
fitted with Newton's method (IRLS) on standardised terms, evaluated on a held-out split: accuracy, ROC AUC,
and false-accept / false-reject rates at each auth level's threshold, plus
the acceptance rate of every simulated attack scenario.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from .features import FEATURES
from .model import FusionModel, expand
from .synth import SCENARIOS, generate

THRESHOLDS = {"level1": 0.5, "level2": 0.8, "level3": 0.9}
PERSONAL_WEIGHT = 3.0  # a real sample from this device counts as three simulated ones


def fit_logreg(X: np.ndarray, y: np.ndarray, w: np.ndarray | None = None, l2: float = 1.0, iters: int = 30):
    """Returns (mean, scale, coef, intercept)."""
    mean = X.mean(axis=0)
    scale = X.std(axis=0)
    scale[scale < 1e-6] = 1.0
    Z = np.hstack([np.ones((len(X), 1)), (X - mean) / scale])
    sw = np.ones(len(y)) if w is None else w
    reg = np.full(Z.shape[1], l2)
    reg[0] = 0.0  # the intercept is not penalised
    beta = np.zeros(Z.shape[1])
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(Z @ beta, -40, 40)))
        grad = Z.T @ (sw * (p - y)) + reg * beta
        hess = Z.T @ (Z * (sw * p * (1 - p))[:, None]) + np.diag(reg)
        step = np.linalg.solve(hess, grad)
        beta -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return mean, scale, beta[1:], float(beta[0])


def roc_auc(y: np.ndarray, p: np.ndarray) -> float:
    """Probability that a random genuine sample outranks a random impostor (ties count half)."""
    order = np.argsort(p)
    ranks = np.empty(len(p))
    ranks[order] = np.arange(1, len(p) + 1)
    for v in np.unique(p):  # average ranks of ties
        m = p == v
        if m.sum() > 1:
            ranks[m] = ranks[m].mean()
    pos = y == 1
    n_pos, n_neg = pos.sum(), (~pos).sum()
    if not n_pos or not n_neg:
        return float("nan")
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def evaluate(p: np.ndarray, y: np.ndarray, scen: np.ndarray | None = None, thresholds=THRESHOLDS) -> dict:
    pos, neg = y == 1, y == 0
    rep: dict = {"n": int(len(y)), "accuracy": round(float(((p >= 0.5) == pos).mean()), 4), "auc": round(roc_auc(y, p), 4)}
    for name, t in thresholds.items():
        rep[name] = {
            "threshold": t,
            "far": round(float((p[neg] >= t).mean()) if neg.any() else 0.0, 4),  # impostors accepted
            "frr": round(float((p[pos] < t).mean()) if pos.any() else 0.0, 4),  # owner rejected
        }
    if scen is not None:
        rep["scenarios"] = {
            str(s): {
                "label": int(y[scen == s][0]),
                **{name: round(float((p[scen == s] >= t).mean()), 4) for name, t in thresholds.items()},
            }
            for s in SCENARIOS if (scen == s).any()
        }
    return rep


def train(
    personal_X: np.ndarray | None = None,
    personal_y: np.ndarray | None = None,
    n: int = 20000,
    seed: int = 7,
) -> FusionModel:
    X, y, scen = generate(n, seed)
    cut = int(len(y) * 0.8)
    Xtr, ytr, wtr = X[:cut], y[:cut], np.ones(cut)
    n_gen = n_imp = 0
    if personal_X is not None and len(personal_X):
        n_gen, n_imp = int((personal_y == 1).sum()), int((personal_y == 0).sum())
        Xtr = np.vstack([Xtr, personal_X])
        ytr = np.concatenate([ytr, personal_y])
        wtr = np.concatenate([wtr, np.full(len(personal_y), PERSONAL_WEIGHT)])
    mean, scale, coef, b = fit_logreg(expand(Xtr), ytr, wtr)
    model = FusionModel(mean, scale, coef, b)
    report = {"test_simulated": evaluate(model.prob(X[cut:]), y[cut:], scen[cut:])}
    if n_gen and n_imp:
        # fitted on these too, so this is a sanity check, not an unbiased estimate
        report["device_samples"] = evaluate(model.prob(personal_X), personal_y)
    model.meta = {
        "trained": datetime.now().isoformat(timespec="seconds"),
        "samples": {"simulated": int(cut), "device_owner": n_gen, "device_other": n_imp},
        "thresholds": THRESHOLDS,
        "report": report,
    }
    return model


__all__ = ["FEATURES", "THRESHOLDS", "evaluate", "fit_logreg", "roc_auc", "train"]
