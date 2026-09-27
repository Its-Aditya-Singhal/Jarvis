"""Encrypted storage for remembered facts and conversation history.

Text and embeddings are sealed with AES-256-GCM using the Keychain key
(embeddings too: a sentence vector can leak what the sentence says).
Timestamps stay in clear so retention can be enforced without decrypting.
Audio is never stored; history holds only turns addressed to the assistant.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from ..database.db import Database
from ..security.crypto import KeyProvider, seal, unseal

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    created   REAL NOT NULL,
    updated   REAL NOT NULL,
    text      BLOB NOT NULL,
    embedding BLOB,               -- sealed float32 vector, NULL until indexed
    source    TEXT NOT NULL       -- said | suggested | typed
);
CREATE TABLE IF NOT EXISTS history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    role      TEXT NOT NULL,      -- you | assistant
    text      BLOB NOT NULL,
    lang      TEXT NOT NULL,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS history_ts ON history(ts);
"""


@dataclass
class Fact:
    id: int
    text: str
    created: datetime
    updated: datetime
    source: str
    embedding: np.ndarray | None = None


@dataclass
class Turn:
    id: int
    ts: datetime
    role: str
    text: str
    lang: str
    embedding: np.ndarray | None = None


class MemoryStore:
    def __init__(self, db: Database, keys: KeyProvider):
        self.db = db
        self.keys = keys
        db.script(SCHEMA)

    # -- sealing -----------------------------------------------------------------
    def _seal(self, ctx: str, data: bytes) -> bytes:
        return seal(self.keys.get_key(), data, f"memory:{ctx}".encode())

    def _open(self, ctx: str, blob: bytes) -> bytes:
        return unseal(self.keys.get_key(), blob, f"memory:{ctx}".encode())

    def _vec(self, ctx: str, v: np.ndarray | None) -> bytes | None:
        return None if v is None else self._seal(ctx, np.asarray(v, np.float32).tobytes())

    def _unvec(self, ctx: str, blob: bytes | None) -> np.ndarray | None:
        return None if not blob else np.frombuffer(self._open(ctx, blob), np.float32)

    # -- facts --------------------------------------------------------------------
    def add_fact(self, text: str, source: str, embedding: np.ndarray | None = None) -> int:
        now = time.time()
        return self.db.insert(
            "INSERT INTO memories(created, updated, text, embedding, source) VALUES(?, ?, ?, ?, ?)",
            (now, now, self._seal("fact", text.encode()), self._vec("fact_emb", embedding), source),
        )

    def update_fact(self, fact_id: int, text: str, embedding: np.ndarray | None) -> bool:
        rows = self.db.run(
            "UPDATE memories SET text = ?, embedding = ?, updated = ? WHERE id = ? RETURNING id",
            (self._seal("fact", text.encode()), self._vec("fact_emb", embedding), time.time(), fact_id),
        )
        return bool(rows)

    def set_fact_embedding(self, fact_id: int, embedding: np.ndarray) -> None:
        self.db.run("UPDATE memories SET embedding = ? WHERE id = ?", (self._vec("fact_emb", embedding), fact_id))

    def delete_fact(self, fact_id: int) -> bool:
        return bool(self.db.run("DELETE FROM memories WHERE id = ? RETURNING id", (fact_id,)))

    def facts(self) -> list[Fact]:
        rows = self.db.run("SELECT * FROM memories ORDER BY updated DESC")
        return [
            Fact(r["id"], self._open("fact", r["text"]).decode(), datetime.fromtimestamp(r["created"]),
                 datetime.fromtimestamp(r["updated"]), r["source"], self._unvec("fact_emb", r["embedding"]))
            for r in rows
        ]

    # -- history ------------------------------------------------------------------
    def add_turn(self, role: str, text: str, lang: str, embedding: np.ndarray | None = None) -> int:
        return self.db.insert(
            "INSERT INTO history(ts, role, text, lang, embedding) VALUES(?, ?, ?, ?, ?)",
            (time.time(), role, self._seal("turn", text.encode()), lang, self._vec("turn_emb", embedding)),
        )

    def set_turn_embedding(self, turn_id: int, embedding: np.ndarray) -> None:
        self.db.run("UPDATE history SET embedding = ? WHERE id = ?", (self._vec("turn_emb", embedding), turn_id))

    def turns(self, since: float | None = None, until: float | None = None, limit: int = 2000) -> list[Turn]:
        rows = self.db.run(
            "SELECT * FROM history WHERE ts >= ? AND ts < ? ORDER BY ts DESC LIMIT ?",
            (since or 0.0, until or 1e12, limit),
        )
        return [
            Turn(r["id"], datetime.fromtimestamp(r["ts"]), r["role"], self._open("turn", r["text"]).decode(),
                 r["lang"], self._unvec("turn_emb", r["embedding"]))
            for r in rows
        ]

    def delete_turns_before(self, ts: float) -> int:
        return len(self.db.run("DELETE FROM history WHERE ts < ? RETURNING id", (ts,)))

    def clear_facts(self) -> int:
        return len(self.db.run("DELETE FROM memories RETURNING id"))

    def clear_history(self) -> int:
        return len(self.db.run("DELETE FROM history RETURNING id"))

    def counts(self) -> dict[str, int]:
        f = self.db.run("SELECT COUNT(*) AS n FROM memories")[0]["n"]
        h = self.db.run("SELECT COUNT(*) AS n FROM history")[0]["n"]
        return {"facts": int(f), "turns": int(h)}
