"""Encrypted local storage for alarms, timers, calendar events and notes.

Times are stored in clear (the scheduler needs them); anything personal —
alarm labels, event titles, note text — is sealed with AES-256-GCM using the
Keychain key that also protects the biometric templates.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

from cryptography.exceptions import InvalidTag

from ..database.db import Database
from ..security.crypto import KeyProvider, seal, unseal

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS alarms (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,            -- alarm | timer
    due     REAL NOT NULL,            -- epoch seconds
    label   BLOB,
    status  TEXT NOT NULL DEFAULT 'pending',  -- pending | ringing | done | cancelled
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    start     REAL NOT NULL,
    end       REAL NOT NULL,
    title     BLOB NOT NULL,
    apple_uid TEXT,
    created   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    text     BLOB NOT NULL,
    apple_id TEXT,
    created  REAL NOT NULL
);
"""


@dataclass
class Alarm:
    id: int
    kind: str
    due: datetime
    label: str
    status: str


@dataclass
class Event:
    id: int | None
    title: str
    start: datetime
    end: datetime
    source: str = "local"  # local | apple | google
    apple_uid: str | None = None


@dataclass
class Note:
    id: int
    text: str
    created: datetime
    apple_id: str | None


class ToolStore:
    def __init__(self, db: Database, keys: KeyProvider):
        self.db = db
        self.keys = keys
        db.script(SCHEMA)

    def _seal(self, table: str, text: str) -> bytes:
        return seal(self.keys.get_key(), text.encode(), f"tools:{table}".encode())

    def _open(self, table: str, blob: bytes | None) -> str:
        if not blob:
            return ""
        return unseal(self.keys.get_key(), blob, f"tools:{table}".encode()).decode()

    def _readable(self, table: str, rows, make):
        """Rows sealed with a key that is gone (a copied database, a replaced Keychain entry) are
        skipped, not allowed to break every list and the alarm clock."""
        out = []
        for r in rows:
            try:
                out.append(make(r))
            except InvalidTag:
                log.warning("%s row %s can't be decrypted with this Mac's key: skipped", table, r["id"])
        return out

    # -- alarms & timers ---------------------------------------------------------
    def add_alarm(self, kind: str, due: datetime, label: str = "") -> int:
        return self.db.insert(
            "INSERT INTO alarms(kind, due, label, created) VALUES(?, ?, ?, ?)",
            (kind, due.timestamp(), self._seal("alarms", label), time.time()),
        )

    def _alarm(self, r) -> Alarm:
        try:
            label = self._open("alarms", r["label"])
        except InvalidTag:  # the alarm still rings, just without its label
            label = ""
        return Alarm(r["id"], r["kind"], datetime.fromtimestamp(r["due"]), label, r["status"])

    def alarms(self, statuses: tuple[str, ...] = ("pending", "ringing")) -> list[Alarm]:
        marks = ",".join("?" * len(statuses))
        rows = self.db.run(f"SELECT * FROM alarms WHERE status IN ({marks}) ORDER BY due", statuses)
        return [self._alarm(r) for r in rows]

    def due_alarms(self, now: datetime) -> list[Alarm]:
        rows = self.db.run(
            "SELECT * FROM alarms WHERE status = 'pending' AND due <= ? ORDER BY due", (now.timestamp(),)
        )
        return [self._alarm(r) for r in rows]

    def set_alarm_status(self, alarm_id: int, status: str) -> None:
        self.db.run("UPDATE alarms SET status = ? WHERE id = ?", (status, alarm_id))

    def snooze(self, alarm_id: int, due: datetime) -> None:
        self.db.run("UPDATE alarms SET status = 'pending', due = ? WHERE id = ?", (due.timestamp(), alarm_id))

    # -- calendar --------------------------------------------------------------------
    def add_event(self, title: str, start: datetime, end: datetime) -> int:
        return self.db.insert(
            "INSERT INTO events(start, end, title, created) VALUES(?, ?, ?, ?)",
            (start.timestamp(), end.timestamp(), self._seal("events", title), time.time()),
        )

    def events_between(self, a: datetime, b: datetime) -> list[Event]:
        rows = self.db.run(
            "SELECT * FROM events WHERE start >= ? AND start < ? ORDER BY start", (a.timestamp(), b.timestamp())
        )
        return self._readable("events", rows, lambda r: Event(
            r["id"], self._open("events", r["title"]), datetime.fromtimestamp(r["start"]),
            datetime.fromtimestamp(r["end"]), "local", r["apple_uid"]))

    def delete_event(self, event_id: int) -> bool:
        return bool(self.db.run("DELETE FROM events WHERE id = ? RETURNING id", (event_id,)))

    def unsynced_events(self) -> list[Event]:
        rows = self.db.run("SELECT * FROM events WHERE apple_uid IS NULL AND start >= ?", (time.time(),))
        return self._readable("events", rows, lambda r: Event(
            r["id"], self._open("events", r["title"]), datetime.fromtimestamp(r["start"]), datetime.fromtimestamp(r["end"])))

    def set_event_apple(self, event_id: int, uid: str) -> None:
        self.db.run("UPDATE events SET apple_uid = ? WHERE id = ?", (uid, event_id))

    # -- notes -----------------------------------------------------------------------
    def add_note(self, text: str) -> int:
        return self.db.insert(
            "INSERT INTO notes(text, created) VALUES(?, ?)", (self._seal("notes", text), time.time())
        )

    def notes(self, limit: int = 200) -> list[Note]:
        rows = self.db.run("SELECT * FROM notes ORDER BY created DESC LIMIT ?", (limit,))
        return self._readable("notes", rows, lambda r: Note(
            r["id"], self._open("notes", r["text"]), datetime.fromtimestamp(r["created"]), r["apple_id"]))

    def delete_note(self, note_id: int) -> bool:
        return bool(self.db.run("DELETE FROM notes WHERE id = ? RETURNING id", (note_id,)))

    def unsynced_notes(self) -> list[Note]:
        return [n for n in self.notes(1000) if n.apple_id is None]

    def set_note_apple(self, note_id: int, apple_id: str) -> None:
        self.db.run("UPDATE notes SET apple_id = ? WHERE id = ?", (apple_id, note_id))
