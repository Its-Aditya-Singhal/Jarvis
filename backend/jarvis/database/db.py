"""SQLite storage for profile settings, activity and security events.

Biometric templates are deliberately NOT stored here; they live in encrypted
files managed by :mod:`jarvis.security.template_store`.
"""

from __future__ import annotations

import os
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
    voice_conf REAL,
    blocked   INTEGER NOT NULL DEFAULT 0
);
"""


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            # owner-only: the data folder and the database (SQLite gives its journal the same mode)
            folder = Path(path).parent
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(folder, 0o700)
            Path(path).touch(mode=0o600, exist_ok=True)
            os.chmod(path, 0o600)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(security_events)")}
        if "voice_conf" not in cols:  # databases created in phase 1
            self._conn.execute("ALTER TABLE security_events ADD COLUMN voice_conf REAL")

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

    # -- generic access for feature stores (tools etc.) ----------------------
    def run(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            self._conn.commit()
        return rows

    def script(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)
            self._conn.commit()

    def insert(self, sql: str, params: tuple) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
        return int(cur.lastrowid or 0)

    # -- security events ---------------------------------------------------
    def add_security_event(
        self,
        kind: str,
        detail: str,
        face_conf: float | None = None,
        blocked: bool = False,
        voice_conf: float | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO security_events(ts, kind, detail, face_conf, voice_conf, blocked) "
                "VALUES(?,?,?,?,?,?)",
                (time.time(), kind, detail, face_conf, voice_conf, int(blocked)),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def security_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM security_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- privacy ---------------------------------------------------------------
    def tables(self) -> list[str]:
        rows = self.run("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        return [r["name"] for r in rows]

    def count(self, table: str, where: str = "") -> int:
        if table not in self.tables():  # table names can't be bound as parameters
            raise ValueError(f"no table {table}")
        return int(self.run(f"SELECT COUNT(*) AS n FROM {table} {where}")[0]["n"])

    def clear_security_events(self) -> int:
        return len(self.run("DELETE FROM security_events RETURNING id"))

    def wipe(self) -> None:
        """Delete every row of every table and compact the file (factory reset)."""
        with self._lock:
            names = [r["name"] for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            for n in names:
                self._conn.execute(f"DELETE FROM {n}")
            if self._conn.execute("SELECT name FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
                self._conn.execute("DELETE FROM sqlite_sequence")
            self._conn.commit()
            self._conn.execute("VACUUM")  # deleted rows would otherwise linger in free pages

    def close(self) -> None:
        with self._lock:
            self._conn.close()
