"""Train the shipped fusion model and print how it compares with other classifiers.

    backend/.venv/bin/python scripts/train_fusion.py

Writes backend/jarvis/auth/fusion/default_model.json. The comparison with
plain logistic regression, a random forest and gradient boosting needs
scikit-learn (optional: pip install scikit-learn); the app itself only
needs NumPy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import numpy as np  # noqa: E402

from jarvis.auth.fusion.features import FEATURES, without_voice  # noqa: E402
from jarvis.auth.fusion.model import DEFAULT_PATH  # noqa: E402
from jarvis.auth.fusion.synth import generate  # noqa: E402
from jarvis.auth.fusion.train import THRESHOLDS, evaluate, fit_logreg, train  # noqa: E402


def row(name: str, r: dict) -> str:
    lv = "  ".join(f"FAR {r[k]['far']:.3f} FRR {r[k]['frr']:.3f}" for k in THRESHOLDS)
    return f"{name:28s} acc {r['accuracy']:.3f}  AUC {r['auc']:.4f}  {lv}"


def gated(X: np.ndarray, p: np.ndarray, level: str) -> np.ndarray:
    """The app never uses the fusion score alone: it also requires a face match
    and a passed liveness challenge, and level 2+ a matching voice in the
    last minute (so a silent owner is capped at level 1 by design). Level 1
    uses the presence score (voice evidence removed), so someone else talking
    while the owner sits at the screen still allows reading."""
    f = {k: X[:, i] for i, k in enumerate(FEATURES)}
    ok = (f["face_sim"] >= 0.42) & (f["live_fresh"] > 0)
    if level != "level1":
        ok &= (f["voice_present"] == 1) & (f["voice_sim"] >= 0.5)
    return np.where(ok, p, 0.0)


def main() -> None:
    model = train()
    model.save(DEFAULT_PATH)
    rep = model.meta["report"]["test_simulated"]
    print(f"saved {DEFAULT_PATH.relative_to(Path.cwd()) if DEFAULT_PATH.is_relative_to(Path.cwd()) else DEFAULT_PATH}\n")
    print("thresholds:", ", ".join(f"{k} {v}" for k, v in THRESHOLDS.items()), "\n")

    X, y, s = generate()
    cut = int(len(y) * 0.8)
    Xte, yte, ste = X[cut:], y[cut:], s[cut:]
    print(row("poly-2 logistic (shipped)", rep))
    m, sc, c, b = fit_logreg(X[:cut], y[:cut])
    print(row("linear logistic", evaluate(1 / (1 + np.exp(-(((Xte - m) / sc) @ c + b))), yte)))
    try:
        from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier

        for name, clf in [
            ("random forest (200 trees)", RandomForestClassifier(200, min_samples_leaf=5, n_jobs=-1, random_state=0)),
            ("gradient boosting", GradientBoostingClassifier(random_state=0)),
        ]:
            clf.fit(X[:cut], y[:cut])
            print(row(name, evaluate(clf.predict_proba(Xte)[:, 1], yte)))
    except ImportError:
        print("(install scikit-learn to compare with a random forest and gradient boosting)")

    p = model.prob(Xte)
    presence = model.prob(without_voice(Xte))
    print("\nacceptance per scenario (fusion score alone -> with the app's hard gates)")
    for name, sr in rep["scenarios"].items():
        m_ = ste == name
        cells = []
        for lv, t in THRESHOLDS.items():
            score = presence if lv == "level1" else p
            g = float((gated(Xte[m_], score[m_], lv) >= t).mean())
            cells.append(f"{lv} {sr[lv]:6.1%} -> {g:6.1%}")
        print(f"  {'owner' if sr['label'] else 'other'}  {name:22s} " + "  ".join(cells))
    print("  (someone_else_speaks passes level 1 by design: the owner is at the screen and may read;"
          " commands in the other voice are blocked at levels 2-3)")
    print("\n" + json.dumps({k: v for k, v in model.meta.items() if k != "report"}))


if __name__ == "__main__":
    main()
