"""Optional sync with Apple Calendar and Apple Notes (AppleScript).

Off by default; the owner turns it on in Settings. When on:
  * new calendar events are also created in the chosen Apple calendar, and
    "what's on my calendar" includes events from all Apple calendars;
  * new notes are also created in an Apple Notes folder ("JARVIS"), and note
    search also looks at Apple Notes titles and that folder.
Syncing through Apple's apps means iCloud can carry these items to the
owner's other devices; that is the point of turning it on.

Values are passed to AppleScript as ``argv`` — never pasted into the script —
so a title like `" & do shell script "…` stays plain text. macOS asks once
for permission ("… wants to control Calendar"); a refusal is reported as a
clear error, not a crash.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

NOTES_FOLDER = "JARVIS"
TIMEOUT_S = 25


class AppleError(RuntimeError):
    pass


def osascript(script: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ["osascript", "-", *args], input=script, capture_output=True, text=True, timeout=TIMEOUT_S
        )
    except subprocess.TimeoutExpired as exc:
        raise AppleError("the Apple app didn't respond in time") from exc
    if out.returncode != 0:
        err = out.stderr.strip()
        if "-1743" in err or "Not authorized" in err or "not allowed" in err.lower():
            raise AppleError(
                "not allowed to control the app — enable it in System Settings → Privacy & Security → Automation"
            )
        raise AppleError(err.splitlines()[-1] if err else "AppleScript failed")
    return out.stdout.rstrip("\n")


# AppleScript date from numeric parts (locale-independent)
_MKDATE = """
on mkdate(y, mo, d, h, mi)
    set t to current date
    set day of t to 1
    set year of t to (y as integer)
    set month of t to (mo as integer)
    set day of t to (d as integer)
    set hours of t to (h as integer)
    set minutes of t to (mi as integer)
    set seconds of t to 0
    return t
end mkdate
"""


def _parts(t: datetime) -> list[str]:
    return [str(t.year), str(t.month), str(t.day), str(t.hour), str(t.minute)]


@dataclass
class AppleEvent:
    title: str
    start: datetime
    calendar: str
    uid: str


class AppleBridge:
    def __init__(self, run=osascript):
        self.run = run

    # -- calendar ------------------------------------------------------------------
    def calendars(self) -> list[str]:
        out = self.run(
            """
tell application "Calendar"
    set out to ""
    repeat with c in calendars
        if writable of c then set out to out & (name of c) & linefeed
    end repeat
    return out
end tell"""
        )
        return [line for line in out.splitlines() if line]

    def create_event(self, calendar: str, title: str, start: datetime, end: datetime) -> str:
        script = _MKDATE + """
on run argv
    set sd to mkdate(item 3 of argv, item 4 of argv, item 5 of argv, item 6 of argv, item 7 of argv)
    set ed to mkdate(item 8 of argv, item 9 of argv, item 10 of argv, item 11 of argv, item 12 of argv)
    tell application "Calendar"
        tell calendar (item 1 of argv)
            set e to make new event with properties {summary:(item 2 of argv), start date:sd, end date:ed}
            return uid of e
        end tell
    end tell
end run"""
        return self.run(script, calendar, title, *_parts(start), *_parts(end))

    def events_on(self, day: date) -> list[AppleEvent]:
        d0 = datetime.combine(day, datetime.min.time())
        d1 = d0 + timedelta(days=1)
        script = _MKDATE + """
on run argv
    set d0 to mkdate(item 1 of argv, item 2 of argv, item 3 of argv, 0, 0)
    set d1 to mkdate(item 4 of argv, item 5 of argv, item 6 of argv, 0, 0)
    set out to ""
    tell application "Calendar"
        repeat with c in calendars
            set evs to (every event of c whose start date ≥ d0 and start date < d1)
            repeat with e in evs
                set s to (start date of e) as «class isot» as string
                set out to out & (summary of e) & tab & s & tab & (name of c) & tab & (uid of e) & linefeed
            end repeat
        end repeat
    end tell
    return out
end run"""
        out = self.run(script, str(d0.year), str(d0.month), str(d0.day), str(d1.year), str(d1.month), str(d1.day))
        events = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            try:
                start = datetime.fromisoformat(parts[1][:16])
            except ValueError:
                continue
            events.append(AppleEvent(parts[0], start, parts[2], parts[3]))
        return sorted(events, key=lambda e: e.start)

    # -- notes -----------------------------------------------------------------------
    def create_note(self, text: str) -> str:
        script = """
on run argv
    tell application "Notes"
        if not (exists folder (item 1 of argv)) then make new folder with properties {name:(item 1 of argv)}
        set n to make new note at folder (item 1 of argv) with properties {body:(item 2 of argv)}
        return id of n
    end tell
end run"""
        # Notes bodies are HTML: escape markup so the text stays literal
        body = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return self.run(script, NOTES_FOLDER, body)

    def search_notes(self, query: str, limit: int = 5) -> list[str]:
        script = """
on run argv
    set q to item 1 of argv
    set out to ""
    tell application "Notes"
        set found to (every note whose name contains q)
        if exists folder (item 2 of argv) then
            set found to found & (every note of folder (item 2 of argv) whose plaintext contains q)
        end if
        repeat with n in found
            set out to out & (name of n) & linefeed
        end repeat
    end tell
    return out
end run"""
        names: list[str] = []
        for line in self.run(script, query, NOTES_FOLDER).splitlines():
            if line and line not in names:
                names.append(line)
        return names[:limit]
