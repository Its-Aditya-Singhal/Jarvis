"""Feature vectors observed on this device, used to personalise the fusion model.

Stored: the nine scores/ages of ``features.FEATURES`` plus a label and the
situation that produced it. Never embeddings, images or audio. Owner samples
come from commands the owner actually carried out; "other" samples from
strangers, spoof alarms and commands in an unrecognised voice.
"""

from __future__ import annotations

import json
import time

import numpy as np

from ...database.db import Database
from .features import FEATURES

SCHEMA = """
CREATE TABLE IF NOT EXISTS fusion_samples (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    label    INTEGER NOT NULL,   -- 1 owner in control, 0 someone/something else
    source   TEXT NOT NULL,
    features TEXT NOT NULL       -- JSON list in FEATURES order
);
"""
MAX_SAMPLES = 5000
MIN_GAP_S = 30.0  # per source: consecutive frames are near-duplicates


class SampleStore:
    def __init__(self, db: Database):
        self.db = db
        db.script(SCHEMA)
        self._last: dict[str, float] = {}

    def add(self, x: np.ndarray, label: int, source: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if now - self._last.get(source, -1e9) < MIN_GAP_S:
            return False
        self._last[source] = now
        self.db.insert(
            "INSERT INTO fusion_samples(ts, label, source, features) VALUES(?, ?, ?, ?)",
            (time.time(), int(label), source, json.dumps([round(float(v), 4) for v in x])),
        )
        self.db.run(
            "DELETE FROM fusion_samples WHERE id NOT IN (SELECT id FROM fusion_samples ORDER BY id DESC LIMIT ?)",
            (MAX_SAMPLES,),
        )
        return True

    def counts(self) -> dict[str, int]:
        rows = self.db.run("SELECT label, COUNT(*) AS n FROM fusion_samples GROUP BY label")
        c = {int(r["label"]): int(r["n"]) for r in rows}
        return {"owner": c.get(1, 0), "other": c.get(0, 0)}

    def load(self) -> tuple[np.ndarray, np.ndarray]:
        rows = self.db.run("SELECT label, features FROM fusion_samples")
        pairs = [(json.loads(r["features"]), r["label"]) for r in rows]
        pairs = [(x, lab) for x, lab in pairs if len(x) == len(FEATURES)]
        X = [x for x, _ in pairs]
        y = [lab for _, lab in pairs]
        return np.array(X, dtype=np.float64).reshape(-1, len(FEATURES)), np.array(y, dtype=np.float64)

    def clear(self) -> None:
        self.db.run("DELETE FROM fusion_samples")
