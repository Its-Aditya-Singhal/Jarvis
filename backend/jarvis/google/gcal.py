"""Google Calendar (the primary calendar): list a day's events and add events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .auth import GoogleAuth

API = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


@dataclass
class GEvent:
    id: str
    title: str
    start: datetime  # local, naive (like the app's own events)
    end: datetime
    all_day: bool = False


def _local(value: str) -> datetime:
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


def _rfc3339(t: datetime) -> str:
    """A local naive time with this Mac's UTC offset (no time-zone name needed)."""
    return t.astimezone().isoformat(timespec="seconds")


def _event(e: dict) -> GEvent | None:
    s, f = e.get("start") or {}, e.get("end") or {}
    try:
        if s.get("dateTime"):
            start = _local(str(s["dateTime"]))
            end = _local(str(f.get("dateTime") or s["dateTime"]))
            return GEvent(str(e.get("id") or ""), str(e.get("summary") or "(no title)"), start, end)
        if s.get("date"):
            start = datetime.combine(date.fromisoformat(str(s["date"])), datetime.min.time())
            return GEvent(str(e.get("id") or ""), str(e.get("summary") or "(no title)"), start,
                          start + timedelta(days=1), all_day=True)
    except ValueError:
        return None
    return None


class GoogleCalendar:
    def __init__(self, auth: GoogleAuth):
        self.auth = auth

    def events_between(self, a: datetime, b: datetime, n: int = 50) -> list[GEvent]:
        r = self.auth.call("GET", API, "calendar", params={
            "timeMin": _rfc3339(a), "timeMax": _rfc3339(b), "singleEvents": "true", "orderBy": "startTime",
            "maxResults": n})
        out = [_event(e) for e in r.json().get("items") or [] if e.get("status") != "cancelled"]
        return [e for e in out if e is not None]

    def events_on(self, day: date) -> list[GEvent]:
        d0 = datetime.combine(day, datetime.min.time())
        return self.events_between(d0, d0 + timedelta(days=1))

    def create(self, title: str, start: datetime, end: datetime) -> str:
        body: dict[str, Any] = {"summary": title, "start": {"dateTime": _rfc3339(start)},
                                "end": {"dateTime": _rfc3339(end)}}
        return str(self.auth.call("POST", API, "calendar", json=body).json().get("id") or "")
