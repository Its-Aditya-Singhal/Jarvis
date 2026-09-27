"""Thread-safe fan-out of events from worker threads to WebSocket clients."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Callable


class EventBus:
    def __init__(self, history: int = 60):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self.activity: deque[dict[str, Any]] = deque(maxlen=history)
        self._handlers: dict[str, list[Callable[[dict[str, Any]], None]]] = {}

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @property
    def has_subscribers(self) -> bool:
        return bool(self._subscribers)

    def on(self, kind: str, handler: Callable[[dict[str, Any]], None]) -> None:
        """Run ``handler`` synchronously (in the publishing thread) for events of ``kind``."""
        self._handlers.setdefault(kind, []).append(handler)

    def publish(self, event: dict[str, Any]) -> None:
        for handler in self._handlers.get(event.get("type", ""), ()):
            try:
                handler(event)
            except Exception:  # a failing listener must not break the publisher
                pass
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._fanout, event)

    def _fanout(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            if q.full():
                # drop the oldest message for slow clients (previews mostly)
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(event)

    def log(self, text: str, level: str = "info") -> None:
        """Human-readable activity-feed entry (never contains biometric values)."""
        entry = {"type": "activity", "ts": time.time(), "text": text, "level": level}
        self.activity.append(entry)
        self.publish(entry)
