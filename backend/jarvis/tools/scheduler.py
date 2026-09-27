"""Rings alarms and timers while JARVIS is running.

A due alarm rings (chime + spoken reminder + macOS notification) every
``ring_every_s`` until dismissed or snoozed, at most ``max_rings`` times.
Alarms that came due while the app was closed are marked missed if they are
more than ``stale_s`` late at startup, instead of all ringing at once.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from .store import Alarm, ToolStore

log = logging.getLogger(__name__)


class AlarmScheduler:
    def __init__(
        self,
        store: ToolStore,
        on_ring: Callable[[Alarm, int], None],
        on_change: Callable[[], None] = lambda: None,
        clock: Callable[[], datetime] = datetime.now,
        ring_every_s: float = 30.0,
        max_rings: int = 6,
        stale_s: float = 600.0,
    ):
        self.store = store
        self.on_ring = on_ring
        self.on_change = on_change
        self.clock = clock
        self.ring_every_s = ring_every_s
        self.max_rings = max_rings
        self.stale_s = stale_s
        self._ringing: dict[int, list] = {}  # id -> [alarm, next_ring_monotonic, count]
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.missed: list[Alarm] = []

    def start(self) -> None:
        now = self.clock()
        for a in self.store.alarms(("pending", "ringing")):
            if a.due < now - timedelta(seconds=self.stale_s):
                self.store.set_alarm_status(a.id, "missed")
                self.missed.append(a)
            elif a.status == "ringing":
                self.store.set_alarm_status(a.id, "pending")
        self._thread = threading.Thread(target=self._loop, name="alarms", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def ringing(self) -> list[Alarm]:
        with self._lock:
            return [v[0] for v in self._ringing.values()]

    def tick(self) -> None:
        now = self.clock()
        changed = False
        for a in self.store.due_alarms(now):
            self.store.set_alarm_status(a.id, "ringing")
            with self._lock:
                self._ringing[a.id] = [a, 0.0, 0]
            changed = True
        mono = time.monotonic()
        with self._lock:
            due = [(k, v) for k, v in self._ringing.items() if mono >= v[1]]
        for alarm_id, entry in due:
            alarm, _, count = entry
            if count >= self.max_rings:  # nobody dismissed it
                self._finish(alarm_id, "done")
                changed = True
                continue
            try:
                self.on_ring(alarm, count)
            except Exception:
                log.exception("alarm ring failed")
            entry[1] = mono + self.ring_every_s
            entry[2] = count + 1
        if changed:
            self.on_change()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("alarm scheduler tick failed")
            self._stop.wait(0.5)

    def _finish(self, alarm_id: int, status: str) -> None:
        with self._lock:
            self._ringing.pop(alarm_id, None)
        self.store.set_alarm_status(alarm_id, status)

    def dismiss(self) -> int:
        ids = [a.id for a in self.ringing()]
        for i in ids:
            self._finish(i, "done")
        if ids:
            self.on_change()
        return len(ids)

    def snooze(self, minutes: int = 5) -> int:
        ids = [a.id for a in self.ringing()]
        due = self.clock() + timedelta(minutes=minutes)
        for i in ids:
            with self._lock:
                self._ringing.pop(i, None)
            self.store.snooze(i, due)
        if ids:
            self.on_change()
        return len(ids)

    def cancel(self, alarm_id: int) -> None:
        with self._lock:
            self._ringing.pop(alarm_id, None)
        self.store.set_alarm_status(alarm_id, "cancelled")
        self.on_change()
