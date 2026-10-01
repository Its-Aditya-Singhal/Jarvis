"""Messages, calls, Contacts, Focus and Music on the Mac.

Reading: the Contacts and Messages databases are opened read-only with SQLite (macOS lets
an app read them only with Full Disk Access, which the owner grants in System Settings).
Contacts fall back to asking the Contacts app (its own, smaller permission) when the
database can't be read.

Sending: an iMessage goes out through the Messages app (AppleScript, values passed as argv,
never pasted into the script); WhatsApp has no scripting interface, so its own
``whatsapp://send`` link opens the chat with the text written in, and Return is pressed only
when WhatsApp is the app in front. Calls open ``tel:`` / ``facetime:`` links, which macOS
places through FaceTime (and an iPhone nearby for phone calls). Nothing here sends or calls by
itself: the runner reads the message back and waits for the owner's "yes" first.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from rapidfuzz import fuzz

log = logging.getLogger(__name__)

HOME = Path.home()
MESSAGES_DB = HOME / "Library" / "Messages" / "chat.db"
ADDRESS_BOOK = HOME / "Library" / "Application Support" / "AddressBook"
APPLE_EPOCH_UNIX = 978307200  # 2001-01-01 00:00 UTC, where Messages counts its dates from
# country calling codes for numbers saved without one (WhatsApp links need the full number)
CALLING_CODES = {"IN": "91", "US": "1", "CA": "1", "GB": "44", "AE": "971", "AU": "61", "DE": "49", "FR": "33",
                 "SG": "65", "NZ": "64", "IE": "353", "NL": "31", "ES": "34", "IT": "39", "JP": "81",
                 "SA": "966", "QA": "974", "NP": "977", "PK": "92", "BD": "880", "LK": "94", "MY": "60", "ZA": "27"}
# quit by focus mode unless the owner names others; never anything they may be working in
DISTRACTING = ("WhatsApp", "Messages", "Discord", "Telegram", "Slack", "Instagram", "Facebook", "Messenger",
               "Twitter", "X", "Netflix", "TV", "Steam", "Reddit", "Signal")
FOCUS_ON, FOCUS_OFF = "JARVIS Focus On", "JARVIS Focus Off"


class AppError(RuntimeError):
    """Something the owner can fix (a permission, a missing app); the text says what."""


@dataclass
class Contact:
    name: str
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)


@dataclass
class Text:
    sender: str  # the contact's name, or the number / address
    handle: str
    text: str
    when: datetime
    unread: bool


def digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def same_number(a: str, b: str) -> bool:
    """+91 98765 43210 and 098765-43210 are the same phone (the last 10 digits match)."""
    da, db = digits(a), digits(b)
    return len(da) >= 7 and len(db) >= 7 and da[-10:] == db[-10:]


def message_text(text: str | None, body: bytes | None) -> str:
    """A message's words: the text column, or (newer macOS) the string inside attributedBody,
    an NSAttributedString archive: b"NSString", 5 marker bytes, a length, then UTF-8."""
    if text:
        return text
    if not body:
        return ""
    i = body.find(b"NSString")
    if i < 0:
        return ""
    j = body.find(b"+", i + 8, i + 16)
    if j < 0:
        return ""
    raw = body[j + 1:]
    if not raw:
        return ""
    if raw[0] == 0x81:
        n, raw = int.from_bytes(raw[1:3], "little"), raw[3:]
    elif raw[0] == 0x82:
        n, raw = int.from_bytes(raw[1:5], "little"), raw[5:]
    else:
        n, raw = raw[0], raw[1:]
    return raw[:n].decode("utf-8", errors="replace").replace("￼", "").strip()


def osascript(script: str, *args: str, timeout: float = 20.0) -> str:
    try:
        out = subprocess.run(["osascript", "-", *args], input=script, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AppError("the app didn't respond in time") from exc
    except FileNotFoundError as exc:
        raise AppError("this only works on a Mac") from exc
    if out.returncode != 0:
        err = out.stderr.strip()
        if "-1743" in err or "not allowed" in err.lower() or "Not authorized" in err:
            raise AppError("macOS hasn't allowed me to control that app: allow it in System Settings, "
                           "Privacy & Security, Automation")
        if "1002" in err:
            raise AppError("macOS hasn't allowed keystrokes: allow JARVIS in System Settings, Privacy & Security, "
                           "Accessibility")
        raise AppError(err.splitlines()[-1] if err else "the app reported an error")
    return out.stdout.rstrip("\n")


class MacApps:
    def __init__(self, run: Callable[..., str] = osascript, opener: Callable[[str], None] | None = None,
                 messages_db: Path = MESSAGES_DB, address_book: Path = ADDRESS_BOOK,
                 front: Callable[[], str | None] = lambda: None, region: Callable[[], str] | None = None,
                 shell: Callable[[list[str]], str] | None = None, sleep: Callable[[float], None] = time.sleep):
        self.run = run
        self._open = opener or (lambda url: subprocess.run(["open", url], check=True, timeout=10, capture_output=True))
        self.messages_db = messages_db
        self.address_book = address_book
        self.front = front
        self._region = region or self._mac_region
        self._shell = shell or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=30,
                                                            check=True).stdout)
        self.sleep = sleep
        self._contacts: tuple[float, list[Contact]] | None = None

    # -- contacts -------------------------------------------------------------------------------
    def _address_dbs(self) -> list[Path]:
        if not self.address_book.is_dir():
            return []
        try:
            return sorted(self.address_book.glob("**/AddressBook-v22.abcddb"))
        except OSError:
            return []

    def _read_contacts(self) -> list[Contact] | None:
        """Every contact from the Contacts databases (None: macOS doesn't let us read them)."""
        dbs = self._address_dbs()
        if not dbs:
            return None
        people: dict[tuple[str, int], Contact] = {}
        readable = False
        for path in dbs:
            try:
                con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            except sqlite3.Error:
                continue
            try:
                rows = con.execute("SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION FROM ZABCDRECORD").fetchall()
                readable = True
                for pk, first, last, nick, org in rows:
                    name = " ".join(p for p in (first, last) if p) or nick or org or ""
                    if name.strip():
                        people[(str(path), pk)] = Contact(name.strip())
                for owner, number in con.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                    if (c := people.get((str(path), owner))) is not None and number:
                        c.phones.append(str(number))
                for owner, addr in con.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                    if (c := people.get((str(path), owner))) is not None and addr:
                        c.emails.append(str(addr).lower())
            except sqlite3.Error as exc:
                log.info("contacts database not readable: %s", exc)
            finally:
                con.close()
        return [c for c in people.values() if c.phones or c.emails] if readable else None

    def contacts(self) -> list[Contact] | None:
        now = time.monotonic()
        if self._contacts is not None and now - self._contacts[0] < 300:
            return self._contacts[1]
        found = self._read_contacts()
        if found is not None:
            self._contacts = (now, found)
        return found

    def find_contact(self, name: str) -> list[Contact]:
        """The contacts best matching a spoken name ("Mom", "Rahul", "Priya Sharma"), best first."""
        name = " ".join(name.split()).strip()
        if not name:
            return []
        people = self.contacts()
        if people is None:
            return self._ask_contacts(name)
        low = name.lower()
        scored = []
        for c in people:
            cl = c.name.lower()
            score = 100 if cl == low else max(fuzz.token_set_ratio(low, cl) - 5, fuzz.WRatio(low, cl) - 10)
            if any(w == low for w in cl.split()):
                score = max(score, 92)  # "Rahul" for "Rahul Verma"
            if score >= 80:
                scored.append((score, c))
        return [c for _, c in sorted(scored, key=lambda x: -x[0])][:5]

    def _ask_contacts(self, name: str) -> list[Contact]:
        """The Contacts app's own search (needs only the Contacts permission)."""
        script = """
on run argv
    set out to ""
    tell application "Contacts"
        repeat with p in (every person whose name contains (item 1 of argv))
            set ph to ""
            repeat with x in phones of p
                set ph to ph & (value of x) & ";"
            end repeat
            set em to ""
            repeat with x in emails of p
                set em to em & (value of x) & ";"
            end repeat
            set out to out & (name of p) & tab & ph & tab & em & linefeed
        end repeat
    end tell
    return out
end run"""
        try:
            out = self.run(script, name)
        except AppError as exc:
            raise AppError(f"I can't read your contacts ({exc})") from exc
        found = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].strip():
                found.append(Contact(parts[0].strip(), [p for p in parts[1].split(";") if p],
                                     [e.lower() for e in parts[2].split(";") if e]))
        return found[:5]

    def name_for(self, handle: str) -> str:
        """A phone number or address as the contact's name (itself when unknown)."""
        people = self.contacts() or []
        h = handle.lower()
        for c in people:
            if h in c.emails or any(same_number(h, p) for p in c.phones):
                return c.name
        return handle

    # -- reading messages -------------------------------------------------------------------------
    def recent_texts(self, contact: Contact | None = None, n: int = 5, since: datetime | None = None) -> list[Text]:
        """The latest messages others sent (newest first), from one contact or anyone."""
        if not self.messages_db.exists():
            raise AppError("I can't find your Messages history on this Mac")
        try:
            con = sqlite3.connect(f"file:{self.messages_db}?mode=ro", uri=True, timeout=3)
        except sqlite3.Error as exc:
            raise AppError("macOS hasn't let me read your messages: turn on JARVIS in System Settings, Privacy & "
                           "Security, Full Disk Access") from exc
        try:
            q = ("SELECT m.text, m.attributedBody, m.date, h.id, m.is_read FROM message m "
                 "JOIN handle h ON m.handle_id = h.ROWID WHERE m.is_from_me = 0 "
                 "AND COALESCE(m.associated_message_type, 0) = 0 AND COALESCE(m.item_type, 0) = 0")
            params: list = []
            if since is not None:
                q += " AND m.date > ?"
                params.append(int((since.timestamp() - APPLE_EPOCH_UNIX) * 1e9))
            q += f" ORDER BY m.date DESC LIMIT {3000 if contact is not None else 400}"  # one person's may be further back
            rows = con.execute(q, params).fetchall()
        except sqlite3.DatabaseError as exc:  # "authorization denied" / "unable to open" without Full Disk Access
            raise AppError("macOS hasn't let me read your messages: turn on JARVIS in System Settings, Privacy & "
                           "Security, Full Disk Access") from exc
        finally:
            con.close()
        out: list[Text] = []
        for text, body, stamp, handle, read in rows:
            if contact is not None and not (str(handle).lower() in contact.emails
                                            or any(same_number(str(handle), p) for p in contact.phones)):
                continue
            words = message_text(text, body)
            if not words:
                continue  # an attachment or reaction with no words
            secs = stamp / 1e9 if stamp and stamp > 1e12 else (stamp or 0)  # nanoseconds since macOS 10.13
            when = datetime.fromtimestamp(secs + APPLE_EPOCH_UNIX)  # seconds since 2001 (UTC) -> local time
            sender = contact.name if contact is not None else self.name_for(str(handle))
            out.append(Text(sender, str(handle), words, when, not read))
            if len(out) >= n:
                break
        return out

    # -- sending -----------------------------------------------------------------------------------
    def send_imessage(self, handle: str, text: str) -> str:
        """Send through Messages: iMessage first, then SMS (an iPhone relaying texts). Returns the service."""
        script = """
on run argv
    tell application "Messages"
        try
            set s to 1st account whose service type = iMessage
            send (item 2 of argv) to participant (item 1 of argv) of s
            return "iMessage"
        on error
            set s to 1st account whose service type = SMS
            send (item 2 of argv) to participant (item 1 of argv) of s
            return "SMS"
        end try
    end tell
end run"""
        return self.run(script, handle, text) or "iMessage"

    def _mac_region(self) -> str:
        try:
            loc = self._shell(["defaults", "read", "-g", "AppleLocale"]).strip()
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
            return ""
        return loc.split("@")[0].split("_")[-1].upper() if "_" in loc else ""

    def international(self, number: str) -> str | None:
        """A phone number with its country code, as digits (WhatsApp's format); None if unknown."""
        n = number.strip()
        d = digits(n)
        if n.startswith("+"):
            return d if len(d) >= 8 else None
        if d.startswith("00"):
            return d[2:] if len(d) >= 10 else None
        code = CALLING_CODES.get(self._region())
        if code is None or len(d) < 7:
            return None
        return code + d.lstrip("0")

    def whatsapp(self, number: str, text: str, press_return: bool = True) -> bool:
        """Open the WhatsApp chat with ``text`` written in; press Return to send it when WhatsApp is
        in front. Returns True when it was sent, False when it waits for the owner to press Return."""
        intl = self.international(number)
        if intl is None:
            raise AppError("I need the number with its country code for WhatsApp; save it like +91 98765 43210")
        try:
            self._open(f"whatsapp://send?phone={intl}&text={quote(text, safe='')}")
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
            raise AppError("WhatsApp isn't installed") from exc
        if not press_return:
            return False
        for _ in range(32):  # up to ~8 s for WhatsApp to come forward with the chat
            if (self.front() or "").lower() == "whatsapp":
                break
            self.sleep(0.25)
        else:
            return False
        self.sleep(1.5)  # the chat and its text box load after the window
        if (self.front() or "").lower() != "whatsapp":
            return False  # never press Return in whatever else came to the front
        try:
            self.run('tell application "System Events" to key code 36')
        except AppError:
            return False
        return True

    def open_link(self, url: str) -> None:
        """Open an app link (spotify:search:…); raises AppError when no app handles it."""
        try:
            self._open(url)
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
            raise AppError("no app opens that link") from exc

    def call(self, number_or_address: str, video: bool = False) -> None:
        target = number_or_address.strip()
        if "@" not in target:
            target = ("+" if target.startswith("+") else "") + digits(target)
        if not target.strip("+"):
            raise AppError("that isn't a number I can call")
        url = ("facetime:" if video else ("facetime-audio:" if "@" in target else "tel:")) + target
        try:
            self._open(url)
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
            raise AppError("FaceTime couldn't start the call") from exc

    # -- focus ---------------------------------------------------------------------------------------
    def shortcuts(self) -> list[str]:
        try:
            return [s.strip() for s in self._shell(["shortcuts", "list"]).splitlines() if s.strip()]
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
            return []

    def set_focus(self, on: bool) -> bool:
        """Turn Do Not Disturb on/off through the owner's shortcut (macOS has no other way for
        apps). False when the shortcut doesn't exist."""
        name = FOCUS_ON if on else FOCUS_OFF
        if name not in self.shortcuts():
            return False
        try:
            self._shell(["shortcuts", "run", name])
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
            raise AppError(f"the shortcut “{name}” failed") from exc
        return True

    # -- music -----------------------------------------------------------------------------------------
    def play_music(self, query: str) -> str | None:
        """Play a playlist, song, artist or album from the Music library. Returns what plays, or None."""
        script = """
on run argv
    set q to item 1 of argv
    tell application "Music"
        try
            set p to (first user playlist whose name is q)
            play p
            return "the playlist " & (name of p)
        end try
        set found to (search library playlist 1 for q)
        if (count of found) is 0 then return ""
        play item 1 of found
        set t to item 1 of found
        return (name of t) & " by " & (artist of t)
    end tell
end run"""
        out = self.run(script, query, timeout=30.0).strip()
        return out or None

    def now_playing(self, player: str) -> str | None:
        script = """
on run argv
    tell application (item 1 of argv)
        if player state is not playing then return ""
        return (name of current track) & " by " & (artist of current track)
    end tell
end run"""
        return self.run(script, player).strip() or None

    def player_volume(self, player: str, level: int | None = None) -> int:
        if level is None:
            script = "on run argv\ntell application (item 1 of argv) to return sound volume\nend run"
            out = self.run(script, player)
        else:
            script = ("on run argv\ntell application (item 1 of argv) to set sound volume to (item 2 of argv as integer)\n"
                      "tell application (item 1 of argv) to return sound volume\nend run")
            out = self.run(script, player, str(max(0, min(100, level))))
        try:
            return int(float(out.strip()))
        except ValueError:
            return level if level is not None else 0
