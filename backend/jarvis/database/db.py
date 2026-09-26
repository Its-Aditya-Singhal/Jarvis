"""SQLite storage for profile settings, activity and security events.

Biometric templates are deliberately NOT stored here; they live in encrypted
files managed by :mod:`jarvis.security.template_store`.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS security_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    kind      TEXT NOT NULL,
    detail    TEXT NOT NULL,
    face_conf REAL,
    blocked   INTEGER NOT NULL DEFAULT 0
);
"""


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # -- profile key/value -------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM profile WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO profile(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM profile WHERE key = ?", (key,))
            self._conn.commit()

    # -- security events ---------------------------------------------------
    def add_security_event(
        self, kind: str, detail: str, face_conf: float | None = None, blocked: bool = False
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO security_events(ts, kind, detail, face_conf, blocked) VALUES(?,?,?,?,?)",
                (time.time(), kind, detail, face_conf, int(blocked)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def security_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM security_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
