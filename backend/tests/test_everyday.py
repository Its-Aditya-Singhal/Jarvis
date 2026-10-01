"""Everyday tools end to end: spoken request -> Brain (scripted model) -> AssistantService (auth levels,
confirmations) -> ToolRunner -> the Mac integrations (fake AppleScript, real SQLite Messages and Contacts
databases, a fake weather service, real files in a fake home folder).

The corpus below is what the owner would say and what a good model returns for it; the model's JSON
still goes through the real parsing, checks and tools. Live routing by the real model is
tests/test_routing_corpus.py (Ollama, or Gemini with JARVIS_GEMINI_KEY).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from jarvis.auth.levels import Trust
from jarvis.brain import Brain
from jarvis.database.db import Database
from jarvis.events import EventBus
from jarvis.fakes import FakeOllama, FakeOllamaServer
from jarvis.llm.fastpath import parse_fast
from jarvis.llm.intents import TOOLS, Action, detect_language
from jarvis.security.crypto import StaticKeyProvider
from jarvis.security.template_store import TemplateStore
from jarvis.service import AssistantService
from jarvis.tools.apps import AppIndex
from jarvis.tools.files import FileSearch, FolderError
from jarvis.tools.mac import MacControl
from jarvis.tools.macapps import APPLE_EPOCH_UNIX, MacApps, message_text, same_number
from jarvis.tools.reading import ReadError, read_text
from jarvis.tools.runner import HARMLESS, LEVELS, ToolRunner
from jarvis.tools.scheduler import AlarmScheduler
from jarvis.tools.store import ToolStore
from jarvis.tools.weather import Weather

NOW = datetime.now().replace(second=0, microsecond=0)
TOMORROW = (NOW + timedelta(days=1)).date()
NEW_TOOLS = ("message.send", "message.read", "call.start", "reminder.add", "reminder.list", "weather", "location.set",
             "web.answer", "files.summarize", "files.move", "files.rename", "calendar.free", "calendar.invite",
             "focus.start", "focus.stop", "music.play", "clipboard.ai", "briefing", "screen.explain")


def iso(d: datetime) -> str:
    return d.isoformat(timespec="minutes")


def at(day, h: int, m: int = 0) -> str:
    return iso(datetime.combine(day, datetime.min.time()) + timedelta(hours=h, minutes=m))


# -- fakes -----------------------------------------------------------------------------------------------
class FakeApp:
    def __init__(self, name):
        self.name, self.quit = name, False

    def localizedName(self): return self.name
    def terminate(self): self.quit = True; return True


class FakeClipboard:
    def __init__(self, text="The quarterly report is due on Friday. Please review the numbers before the meeting."):
        self.value, self.secret = text, False

    def text(self): return self.value
    def concealed(self): return self.secret
    def set_text(self, t): self.value = t


class FakeApple:
    def __init__(self):
        self.reminders_added: list[tuple] = []
        self.items = [("Pay rent", NOW.replace(hour=23, minute=0)), ("Call the plumber", None),
                      ("Renew passport", NOW + timedelta(days=10))]

    def add_reminder(self, text, due=None, list_name=""):
        self.reminders_added.append((text, due, list_name))
        return list_name or "Reminders"

    def reminders(self, limit=40):
        return list(self.items)

    def create_note(self, text): return "note-1"
    def search_notes(self, q, limit=5): return []
    def events_on(self, day): return []


class FakeAI:
    gemini_heavy: str | None = "gemini-3.5-flash-lite"

    def __init__(self):
        self.calls: list[tuple] = []

    def write(self, system, text, max_tokens=700):
        self.calls.append(("write", system, text))
        if "Translate" in system:
            return "रिपोर्ट शुक्रवार तक देनी है।"
        if "Correct the spelling" in system:
            return "The quarterly report is due on Friday."
        return "It's a lease for the flat on MG Road; the notice period is two months."

    def look_up(self, system, question, max_tokens=400):
        self.calls.append(("look_up", system, question))
        return "One US dollar is about 88 rupees today"

    def see(self, system, question, image, mime="image/jpeg", max_tokens=500):
        self.calls.append(("see", system, question, image))
        return ("You're in Xcode and the build failed: the error says the module Foo can't be found, which usually "
                "means a package wasn't added. I can search the web for the fix or open the package settings for you.")


def weather_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding" in request.url.host:
            name = request.url.params.get("name", "")
            if name.lower() == "atlantis":
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"results": [{"name": name.title(), "latitude": 18.5, "longitude": 73.8,
                                                          "country": "India", "admin1": "Maharashtra"}]})
        days = [NOW.date().isoformat(), TOMORROW.isoformat()]
        return httpx.Response(200, json={
            "current": {"temperature_2m": 27.4, "apparent_temperature": 30.1, "weather_code": 2},
            "daily": {"time": days, "temperature_2m_max": [31.2, 29.0], "temperature_2m_min": [22.1, 21.5],
                      "precipitation_probability_max": [40, 80], "weather_code": [2, 63]}})
    return httpx.MockTransport(handler)


def make_messages_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB, date INTEGER, handle_id INTEGER,
                              is_from_me INTEGER, is_read INTEGER, associated_message_type INTEGER, item_type INTEGER);
    """)
    con.executemany("INSERT INTO handle VALUES (?, ?)", [(1, "+919876543210"), (2, "+919811122233"), (3, "priya@example.com")])

    def stamp(d: datetime) -> int:
        return int((d.timestamp() - APPLE_EPOCH_UNIX) * 1e9)

    rows = [
        ("Don't forget to buy milk on the way home", None, NOW - timedelta(minutes=30), 1, 0, 0),
        (None, attributed("Are we still on for dinner at 8?"), NOW - timedelta(minutes=10), 2, 0, 0),
        ("Thanks for the slides!", None, NOW - timedelta(hours=5), 3, 0, 1),
        ("ok", None, NOW - timedelta(minutes=5), 1, 1, 1),  # sent by the owner: never read out
        ("Loved “dinner”", None, NOW - timedelta(minutes=9), 2, 0, 0),  # a reaction
    ]
    for i, (text, body, when, h, mine, read) in enumerate(rows, 1):
        assoc = 2000 if text and text.startswith("Loved") else 0
        con.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)", (i, text, body, stamp(when), h, mine, read, assoc))
    con.commit()
    con.close()


def attributed(text: str) -> bytes:
    """An NSAttributedString archive as Messages stores it (the parts message_text relies on)."""
    raw = text.encode()
    size = bytes([len(raw)]) if len(raw) < 0x80 else b"\x81" + len(raw).to_bytes(2, "little")
    return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85"
            b"\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + size + raw + b"\x86\x84\x02iI\x01\x0c\x92\x84\x84\x84\x0c")


def make_address_book(root: Path) -> None:
    d = root / "Sources" / "ABC-123"
    d.mkdir(parents=True)
    con = sqlite3.connect(d / "AddressBook-v22.abcddb")
    con.executescript("""
        CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZLASTNAME TEXT, ZNICKNAME TEXT, ZORGANIZATION TEXT);
        CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT);
        CREATE TABLE ZABCDEMAILADDRESS (ZOWNER INTEGER, ZADDRESS TEXT);
    """)
    con.executemany("INSERT INTO ZABCDRECORD VALUES (?, ?, ?, ?, ?)", [
        (1, "Mom", None, None, None), (2, "Rahul", "Verma", None, None), (3, "Priya", "Sharma", None, None),
        (4, "Dad", None, None, None), (5, None, None, None, "Acme Corp")])
    con.executemany("INSERT INTO ZABCDPHONENUMBER VALUES (?, ?)", [
        (1, "+91 98765 43210"), (2, "98111 22233"), (4, "098220 12345"), (5, "1800 123 456")])
    con.executemany("INSERT INTO ZABCDEMAILADDRESS VALUES (?, ?)", [(2, "rahul@example.com"), (3, "Priya@Example.com")])
    con.commit()
    con.close()


class World:
    """The fake Mac: everything the tools touched, for the checks."""

    def __init__(self, tmp: Path, settings):
        self.tmp = tmp
        self.home = tmp / "home"
        for f in ("Documents", "Desktop", "Downloads", "Documents/Taxes"):
            (self.home / f).mkdir(parents=True, exist_ok=True)
        self.osa: list[tuple[str, tuple]] = []
        self.urls: list[str] = []
        self.shell: list[list[str]] = []
        self.mac_calls: list[list[str]] = []
        self.front = ["Safari"]
        self.apps = [FakeApp(n) for n in ("Finder", "Safari", "WhatsApp", "Slack", "Spotify", "Music", "Xcode")]
        self.clipboard = FakeClipboard()
        self.apple = FakeApple()
        self.ai = FakeAI()
        self.invites: list[tuple] = []
        self.library = {"believer": "Believer by Imagine Dragons", "workout": "the playlist Workout",
                        "arijit singh": "Tum Hi Ho by Arijit Singh"}
        self.has_focus_shortcuts = True
        make_messages_db(tmp / "chat.db")
        make_address_book(tmp / "AddressBook")
        self._files()

        self.db = Database(settings.db_path)
        self.db.set("file_search_folders", json.dumps([str(self.home / f) for f in ("Documents", "Desktop", "Downloads")]))
        self.store = ToolStore(self.db, StaticKeyProvider())
        self.macapps = MacApps(run=self._osa, opener=self._open, messages_db=tmp / "chat.db",
                               address_book=tmp / "AddressBook", front=lambda: self.front[0], region=lambda: "IN",
                               shell=self._shell, sleep=lambda s: None)
        self.mac = MacControl(runner=self._mac_run, running=lambda: [a for a in self.apps if not a.quit],
                              home=self.home, clipboard=self.clipboard, front=lambda: self.front[0],
                              extra_folders=lambda: FileSearch(self.db).folders(),
                              screen_access=lambda: True)
        world = self

        class Google:
            auth = SimpleNamespace(has=lambda s: True)
            gmail = SimpleNamespace(
                find_address=lambda n: {"rahul": ("Rahul Verma", "rahul@example.com"),
                                        "priya": ("Priya Sharma", "priya@example.com")}.get(n.lower().split()[0])
                if "@" not in n else (n, n.lower()),
                unread=lambda n: (2, False, [SimpleNamespace(sender="Rahul Verma"), SimpleNamespace(sender="Amazon")]))
            calendar = SimpleNamespace(create=lambda *a: world.invites.append(a) or "evt-1", events_on=lambda d: [])

        self.google = Google()

    def _files(self):
        dl, docs, desk = self.home / "Downloads", self.home / "Documents", self.home / "Desktop"
        now = NOW.timestamp()
        for path, age_h, body in [
            (dl / "Lease Agreement.txt", 26, "LEASE. The tenant must give two months' notice. Rent is 25000 a month."),
            (dl / "Invoice 4411.pdf", 25, None),
            (dl / "notes from call.md", 1, "# Call\n- ship on Friday\n- Rahul owns the budget"),
            (desk / "Screenshot 2026-10-01 at 10.00.00.png", 3, None),
            (docs / "Resume Aditya.txt", 400, "Aditya Singhal. Software engineer. Python, Swift, React."),
        ]:
            if body is None:
                path.write_bytes(b"%PDF-1.4 fake" if path.suffix == ".pdf" else b"\x89PNG fake")
            else:
                path.write_text(body)
            os.utime(path, (now - age_h * 3600, now - age_h * 3600))

    # AppleScript, `open`, shell and Mac command stand-ins
    def _osa(self, script: str, *args: str, timeout: float = 20.0) -> str:
        self.osa.append((script, args))
        if "service type = iMessage" in script:
            return "iMessage"
        if "search library playlist" in script:
            return self.library.get(args[0].lower(), "")
        if "current track" in script:
            return "Believer by Imagine Dragons"
        if "sound volume" in script:
            return args[1] if len(args) > 1 else "55"
        return ""

    def _open(self, url: str) -> None:
        self.urls.append(url)
        if url.startswith("whatsapp:"):
            self.front[0] = "WhatsApp"

    def _shell(self, argv: list[str]) -> str:
        self.shell.append(argv)
        if argv[:2] == ["shortcuts", "list"]:
            return "JARVIS Focus On\nJARVIS Focus Off\nOther\n" if self.has_focus_shortcuts else "Other\n"
        return ""

    def _mac_run(self, argv: list[str]) -> str:
        self.mac_calls.append(argv)
        if argv[0] == "screencapture":
            Path(argv[-1]).write_bytes(b"\xff\xd8\xff\xe0 fake jpeg")
        return ""

    def sent(self) -> list[tuple]:
        return [a for s, a in self.osa if "service type = iMessage" in s]


def fake_trust(level=2):
    return Trust(level, 0.99, blockers={} if level >= 2 else {2: "voice_needed", 3: "voice_needed"},
                 l3_ready=level >= 2)


class Scripted(FakeOllama):
    """The model: answers each request from the script (the utterance -> the JSON a good model returns)."""

    def __init__(self, script: dict[str, dict]):
        super().__init__(responder=self.answer)
        self.table = {norm(k): v for k, v in script.items()}

    def answer(self, text, schema):
        return self.table.get(norm(text), {"actions": [], "reply": "Sorry, not in the script."})


def norm(text: str) -> str:
    return re.sub(r"^(?:hey |ok |okay )?jarvis[\s,]*", "", " ".join(text.lower().split())).strip(" ?.!")


@pytest.fixture
def world(tmp_path, settings):
    return World(tmp_path, settings)


def service(world: World, settings, script: dict[str, dict]) -> AssistantService:
    brain = Brain(settings, world.db, names=lambda: ("Jarvis", "Aditya Singhal"), voice_gender=lambda: "female",
                  server=FakeOllamaServer(), client=Scripted(script))

    def tools(svc):
        r = ToolRunner(world.db, world.store, AppIndex([world.tmp / "Applications"]), FileSearch(world.db), world.apple,
                       on_change=svc.tools_changed, mac=world.mac, macapps=world.macapps,
                       weather=Weather(transport=weather_transport()))
        r.google = world.google
        r.ai = world.ai
        r.names = lambda: ("Jarvis", "Aditya Singhal")
        return r, AlarmScheduler(world.store, on_ring=svc.ring)

    (world.tmp / "Applications" / "Spotify.app").mkdir(parents=True, exist_ok=True)
    svc = AssistantService(settings, world.db, TemplateStore(settings.templates_dir, StaticKeyProvider()), EventBus(),
                           Nothing(), Nothing(), brain_factory=lambda s: brain, tools_factory=tools)
    svc.trust = lambda now=None, screen=False: fake_trust(2)
    return svc


class Nothing:
    ready, error, status = True, None, "active"
    def load(self): return True
    def start(self): ...
    def stop(self): ...
    def latest(self, max_age_s=1.0): return None
    def analyze(self, frame): return []


def A(tool: str, **args) -> dict:
    return {"tool": tool, "args": args}


# -- the corpus ------------------------------------------------------------------------------------------
# (what the owner says, what the model returns, the outcome of each action: ok | fail | confirm (then
#  confirmed by click), and a check on the fake Mac afterwards)
D = TOMORROW
CORPUS: list[tuple[str, list[dict], list[str], object]] = [
    # messages: read back first, sent only after the confirmation
    ("Text mom that I'll be home by 8", [A("message.send", to="Mom", text="I'll be home by 8")], ["confirm"],
     lambda w: w.sent()[-1] == ("+91 98765 43210", "I'll be home by 8")),
    ("send a message to Rahul saying the meeting moved to 4", [A("message.send", to="Rahul", text="The meeting moved to 4")],
     ["confirm"], lambda w: w.sent()[-1] == ("98111 22233", "The meeting moved to 4")),
    ("iMessage Priya happy birthday", [A("message.send", to="Priya", text="Happy birthday!")], ["confirm"],
     lambda w: w.sent()[-1] == ("priya@example.com", "Happy birthday!")),
    ("message 98111 22233 saying call me", [A("message.send", to="98111 22233", text="Call me")], ["confirm"],
     lambda w: w.sent()[-1][0] == "98111 22233"),
    ("whatsapp Rahul that I'm on my way", [A("message.send", to="Rahul", text="I'm on my way", app="whatsapp")], ["confirm"],
     lambda w: w.urls[-1] == "whatsapp://send?phone=919811122233&text=I%27m%20on%20my%20way"
     and any("key code 36" in s for s, _ in w.osa)),
    ("send a whatsapp to mom saying call me when you're free",
     [A("message.send", to="mom", text="Call me when you're free", app="WhatsApp")], ["confirm"],
     lambda w: "phone=919876543210" in w.urls[-1]),
    ("tell dad on whatsapp that I reached safely", [A("message.send", to="Dad", text="I reached safely", app="whatsapp")],
     ["confirm"], lambda w: "phone=919822012345" in w.urls[-1]),  # 098220 12345 saved without +91
    ("priya ko message karo ki main 10 minute mein aa raha hoon",
     [A("message.send", to="Priya", text="Main 10 minute mein aa raha hoon")], ["confirm"],
     lambda w: w.sent()[-1] == ("priya@example.com", "Main 10 minute mein aa raha hoon")),
    ("message Zoya that I'm late", [A("message.send", to="Zoya", text="I'm late")], ["fail"], None),
    ("send a message to Rahul", [A("message.send", to="Rahul", text="")], ["fail"], None),
    ("whatsapp priya@example.com hi", [A("message.send", to="priya@example.com", text="Hi", app="whatsapp")], ["fail"], None),
    ("read my messages", [A("message.read")], ["ok"], None),
    ("reply to him saying yes, 8 works", [A("message.send", to="him", text="Yes, 8 works")], ["confirm"],
     lambda w: w.sent()[-1] == ("+919811122233", "Yes, 8 works")),  # Rahul's message was the newest one read out
    ("what did mom text me", [A("message.read", **{"from": "Mom"})], ["ok"], None),
    ("did Rahul message me", [A("message.read", **{"from": "Rahul", "count": "3"})], ["ok"], None),
    ("read the last message from Priya", [A("message.read", **{"from": "Priya", "count": 1})], ["ok"], None),
    ("mere naye messages padho", [A("message.read")], ["ok"], None),
    ("any texts from Zoya?", [A("message.read", **{"from": "Zoya"})], ["fail"], None),
    # calls
    ("call dad", [A("call.start", to="Dad")], ["confirm"], lambda w: w.urls[-1] == "tel:09822012345"),
    ("ring Rahul", [A("call.start", to="Rahul Verma")], ["confirm"], lambda w: w.urls[-1] == "tel:9811122233"),
    ("FaceTime Priya", [A("call.start", to="Priya", video=True)], ["confirm"], lambda w: w.urls[-1] == "facetime:priya@example.com"),
    ("video call mom", [A("call.start", to="Mom", video="true")], ["confirm"], lambda w: w.urls[-1] == "facetime:+919876543210"),
    ("mummy ko call karo", [A("call.start", to="Mom")], ["confirm"], lambda w: w.urls[-1] == "tel:+919876543210"),
    ("call Acme", [A("call.start", to="Acme Corp")], ["confirm"], lambda w: w.urls[-1] == "tel:1800123456"),
    ("call +91 98765 43210", [A("call.start", to="+91 98765 43210")], ["confirm"], lambda w: w.urls[-1] == "tel:+919876543210"),
    # reminders and notes
    ("remind me to call the bank tomorrow at 10", [A("reminder.add", text="Call the bank", due=at(D, 10))], ["ok"],
     lambda w: w.apple.reminders_added[-1][0] == "Call the bank" and w.apple.reminders_added[-1][1].hour == 10),
    ("add pay the electricity bill to my reminders", [A("reminder.add", text="Pay the electricity bill")], ["ok"],
     lambda w: w.apple.reminders_added[-1] == ("Pay the electricity bill", None, "")),
    ("put buy eggs on my shopping list", [A("reminder.add", text="Buy eggs", list="Shopping")], ["ok"],
     lambda w: w.apple.reminders_added[-1][2] == "Shopping"),
    ("remind me to take my medicine at 9 pm", [A("reminder.add", text="Take my medicine", due=at(D, 21))], ["ok"], None),
    ("kal subah dentist ko phone karna yaad dila dena", [A("reminder.add", text="Dentist ko phone karna", due=at(D, 9))],
     ["ok"], None),
    ("remind me to stretch yesterday", [A("reminder.add", text="Stretch", due=iso(NOW - timedelta(days=1)))], ["fail"], None),
    ("add milk to my reminders", [A("reminder.add", text="Milk")], ["ok"], None),
    ("what are my reminders for today", [A("reminder.list")], ["ok"], None),
    ("read my reminders", [A("reminder.list", when="all")], ["ok"], None),
    ("what's on my to do list", [A("reminder.list", when="all")], ["ok"], None),
    ("add a note in Apple Notes saying gate code is 4512", [A("notes.add", text="Gate code is 4512", app="apple")], ["ok"], None),
    ("put this in my apple notes: call the plumber on Monday", [A("notes.add", text="Call the plumber on Monday", app="Apple Notes")],
     ["ok"], None),
    # weather and the web
    ("my city is Pune", [A("location.set", city="Pune")], ["ok"], lambda w: w.db.get("home_city") == "Pune"),
    ("how's the weather today", [A("weather")], ["ok"], None),
    ("will it rain tomorrow", [A("weather", day="tomorrow")], ["ok"], None),
    ("what's the temperature in Mumbai", [A("weather", place="Mumbai")], ["ok"], None),
    ("do I need an umbrella today", [A("weather", day="today")], ["ok"], None),
    ("kal mausam kaisa rahega", [A("weather", day="tomorrow")], ["ok"], None),
    ("what's the weather in Atlantis", [A("weather", place="Atlantis")], ["fail"], None),
    ("I live in Bangalore, use that for the weather", [A("location.set", city="Bangalore")], ["ok"],
     lambda w: w.db.get("home_city") == "Bangalore"),
    ("what's the news today", [A("web.answer", question="What are today's top news headlines?")], ["ok"], None),
    ("who won the match last night", [A("web.answer", question="Who won last night's cricket match?")], ["ok"], None),
    ("how much is 100 dollars in rupees", [A("web.answer", question="How much is 100 US dollars in Indian rupees today?")],
     ["ok"], None),
    ("convert 50 euros to rupees", [A("web.answer", question="50 euros in Indian rupees")], ["ok"], None),
    ("what's the price of bitcoin right now", [A("web.answer", question="Bitcoin price now")], ["ok"], None),
    ("aaj ki taaza khabar kya hai", [A("web.answer", question="Aaj ki top news India")], ["ok"], None),
    # files
    ("summarise the lease I downloaded yesterday", [A("files.summarize", kind="document", when="yesterday", query="lease")],
     ["ok"], lambda w: "two months" in w.ai.calls[-1][2]),
    ("read the lease agreement and tell me the notice period",
     [A("files.summarize", query="lease", question="What is the notice period?")], ["ok"],
     lambda w: "notice period" in w.ai.calls[-1][1]),
    ("what's in my latest download", [A("files.summarize", folder="downloads")], ["ok"],
     lambda w: "ship on Friday" in w.ai.calls[-1][2]),
    ("summarize my resume", [A("files.summarize", query="resume")], ["ok"], None),
    ("summarise the invoice pdf", [A("files.summarize", kind="pdf", query="invoice")], ["fail"], None),  # not a real PDF
    ("summarise the screenshot on my desktop", [A("files.summarize", kind="screenshot")], ["fail"], None),
    ("move the lease to documents", [A("files.move", query="lease", to="Documents")], ["confirm"],
     lambda w: (w.home / "Documents" / "Lease Agreement.txt").exists()),
    ("move my latest screenshot to the downloads folder", [A("files.move", kind="screenshot", to="downloads")], ["confirm"],
     lambda w: any(p.name.startswith("Screenshot") for p in (w.home / "Downloads").iterdir())),
    ("find yesterday's invoice and put it in my taxes folder",
     [A("files.recent", query="invoice", when="yesterday"), A("files.move", to="Taxes")], ["ok", "confirm"],
     lambda w: (w.home / "Documents" / "Taxes" / "Invoice 4411.pdf").exists()),
    ("rename my latest download to call notes", [A("files.rename", folder="downloads", name="call notes")], ["confirm"],
     lambda w: (w.home / "Downloads" / "call notes.md").exists()),
    ("rename it to ../../etc/passwd", [A("files.rename", name="../../etc/passwd")], ["confirm"],
     lambda w: (w.home / "Downloads" / "etc passwd.md").exists()),  # slashes and dots can't leave the folder
    ("move the resume to the system folder", [A("files.move", query="resume", to="System")], ["fail"], None),
    ("move my resume to documents", [A("files.move", query="resume", to="Documents")], ["fail"], None),  # already there
    # calendar
    ("what's my day look like", [A("calendar.list", date=NOW.date().isoformat())], ["ok"], None),
    ("when am I free tomorrow", [A("calendar.free", date=D.isoformat())], ["ok"], None),
    ("do I have a free hour tomorrow afternoon", [A("calendar.free", date=D.isoformat(), minutes=60, after="12:00", before="18:00")],
     ["ok"], None),
    ("find me a 30 minute slot tomorrow morning", [A("calendar.free", date=D.isoformat(), minutes="30", after="9", before="12")],
     ["ok"], None),
    ("set up a meeting with Rahul tomorrow at 3", [A("calendar.invite", title="Meeting with Rahul", start=at(D, 15), **{"with": "Rahul"})],
     ["confirm"], lambda w: w.invites[-1][3] == ["rahul@example.com"]),
    ("book a 30 minute sync with Rahul and Priya tomorrow at 4 pm",
     [A("calendar.invite", title="Sync", start=at(D, 16), end=at(D, 16, 30), **{"with": "Rahul, Priya"})], ["confirm"],
     lambda w: w.invites[-1][3] == ["rahul@example.com", "priya@example.com"] and w.invites[-1][2].minute == 30),
    ("schedule a call with zed@example.com tomorrow at 11 am",
     [A("calendar.invite", title="Call", start=at(D, 11), **{"with": ["zed@example.com"]})], ["confirm"],
     lambda w: w.invites[-1][3] == ["zed@example.com"]),
    ("set up a meeting with Zoya tomorrow", [A("calendar.invite", title="Meeting", start=at(D, 10), **{"with": "Zoya"})],
     ["fail"], None),
    # focus
    ("start focus mode", [A("focus.start")], ["ok"],
     lambda w: ["shortcuts", "run", "JARVIS Focus On"] in w.shell and any(a.quit for a in w.apps if a.name == "WhatsApp")),
    ("focus mode for an hour", [A("focus.start", minutes=60)], ["ok"], None),
    ("I need to focus for 45 minutes, close safari too", [A("focus.start", minutes="45", close=["Safari"])], ["ok"],
     lambda w: next(a for a in w.apps if a.name == "Safari").quit),
    ("end focus mode", [A("focus.stop")], ["ok"], lambda w: ["shortcuts", "run", "JARVIS Focus Off"] in w.shell),
    ("turn on do not disturb", [A("focus.start", dnd_only=True)], ["ok"], None),
    # music
    ("play Believer", [A("music.play", query="Believer")], ["ok"], None),
    ("play my workout playlist", [A("music.play", query="Workout")], ["ok"], None),
    ("arijit singh ke gaane bajao", [A("music.play", query="Arijit Singh")], ["ok"], None),
    ("play Midnights by Taylor Swift", [A("music.play", query="Midnights Taylor Swift")], ["ok"],
     lambda w: w.urls[-1] == "spotify:search:Midnights%20Taylor%20Swift"),  # not in the library: Spotify search
    ("play some lofi beats with spotify", [A("music.play", query="lofi beats", app="spotify")], ["ok"],
     lambda w: w.urls[-1] == "spotify:search:lofi%20beats"),
    ("what's playing", [A("media.control", action="now")], ["ok"], None),
    ("set Spotify's volume to 30", [A("media.control", action="volume", level=30)], ["ok"], None),
    # clipboard
    ("summarise what I copied", [A("clipboard.ai", task="summarize")], ["ok"], None),
    ("translate my clipboard into Hindi", [A("clipboard.ai", task="translate", to="Hindi")], ["ok"],
     lambda w: w.clipboard.value == "रिपोर्ट शुक्रवार तक देनी है।"),
    ("explain what I just copied", [A("clipboard.ai", task="explain")], ["ok"], None),
    ("fix the grammar of the text I copied", [A("clipboard.ai", task="proofread")], ["ok"],
     lambda w: w.clipboard.value == "The quarterly report is due on Friday."),
    # briefing
    ("give me my daily briefing", [A("briefing")], ["ok"], None),
    ("good morning, brief me", [A("briefing")], ["ok"], None),
    ("aaj ka briefing do", [A("briefing")], ["ok"], None),
    # screen help
    ("Jarvis, what's happening on my screen", [A("screen.explain")], ["ok"],
     lambda w: w.ai.calls[-1][0] == "see" and w.ai.calls[-1][3].startswith(b"\xff\xd8")
     and not list(Path(os.environ.get("TMPDIR", "/tmp")).glob("jarvis-screen-*"))),
    ("what does this error mean", [A("screen.explain", question="What does this error mean?")], ["ok"], None),
    ("meri screen pe kya ho raha hai", [A("screen.explain")], ["ok"], None),
    # several at once
    ("add buy milk to my reminders and play my workout playlist",
     [A("reminder.add", text="Buy milk"), A("music.play", query="Workout")], ["ok", "ok"], None),
    ("what's the weather and what's on my calendar today", [A("weather"), A("calendar.list", date=NOW.date().isoformat())],
     ["ok", "ok"], None),
]


def run_one(svc: AssistantService, world: World, text: str, outcomes: list[str], verdict: str = "verified") -> tuple[dict, list]:
    out = svc.command(text, detect_language(text), source="voice", verdict=verdict)
    got = []
    for a in out["actions"]:
        if a.get("data", {}).get("pending"):
            got.append("confirm")
        else:
            got.append("ok" if a.get("ok") else "fail")
    return out, got


def test_the_everyday_corpus_through_the_real_command_path(world, settings):
    svc = service(world, settings, {t: {"actions": acts, "reply": ""} for t, acts, _, _ in CORPUS})
    world.db.set("home_city", "")
    yesterday10 = datetime.combine(TOMORROW, datetime.min.time()) + timedelta(hours=10)
    world.store.add_event("Standup", yesterday10, yesterday10 + timedelta(minutes=30))
    world.store.add_event("Design review", yesterday10 + timedelta(hours=4), yesterday10 + timedelta(hours=5, minutes=30))
    problems = []
    for text, _acts, want, check in CORPUS:
        assert parse_fast(norm(text), detect_language(text), NOW, lambda n: False) is None or text.startswith("remember"), text
        if text.startswith("rename it to"):
            svc.tools.last_files = [world.home / "Downloads" / "call notes.md"]
        out, got = run_one(svc, world, text, want)
        if got != want:
            problems.append(f"{text!r}: wanted {want}, got {got}: {out['reply']}")
            continue
        for a in out["actions"]:
            if pid := a.get("data", {}).get("pending"):
                res = svc.confirm(pid, True, "click")
                if not res["ok"]:
                    problems.append(f"{text!r}: confirmed but failed: {res['reply']}")
        if check is not None and not check(world):
            problems.append(f"{text!r}: the check failed; reply {out['reply']!r}")
        if not out["reply"].strip() or "Something went wrong" in out["reply"]:
            problems.append(f"{text!r}: bad reply {out['reply']!r}")
    assert not problems, "\n".join(problems)
    assert len(CORPUS) >= 90


@pytest.mark.parametrize("text,acts,want,check", [c for c in CORPUS if any(LEVELS[a["tool"]] >= 2 for a in c[1])],
                         ids=lambda v: v if isinstance(v, str) else "")
def test_an_unverified_voice_gets_nothing_beyond_level_1(world, settings, text, acts, want, check):
    """A voice that didn't match the owner: no message, call, file, clipboard, screen or mail."""
    svc = service(world, settings, {text: {"actions": acts, "reply": ""}})
    out, got = run_one(svc, world, text, want, verdict="uncertain")
    for a, g in zip(acts, got):
        if LEVELS[a["tool"]] >= 2:
            assert g == "fail", (a, out["reply"])
    assert not world.sent() and not world.invites and not world.ai.calls
    assert not any(u.startswith(("tel:", "facetime", "whatsapp:")) for u in world.urls)
    assert svc.pending() is None


def test_every_everyday_tool_is_catalogued_with_a_level_and_described():
    from jarvis.llm.intents import describe

    for t in NEW_TOOLS:
        assert t in TOOLS and t in LEVELS, t
        assert describe(Action(t, {}), "en", NOW) and describe(Action(t, {}), "hi", NOW)
    for t in ("message.send", "call.start", "files.move", "files.rename", "calendar.invite"):
        assert LEVELS[t] == 3  # reaches other people or changes a file: read back, then confirmed
    for t in ("message.read", "files.summarize", "clipboard.ai", "briefing", "screen.explain"):
        assert LEVELS[t] == 2 and t not in HARMLESS  # reads private things (and sends them to the AI)


def test_saying_no_sends_nothing(world, settings):
    svc = service(world, settings, {"text mom hi": {"actions": [A("message.send", to="Mom", text="Hi")], "reply": ""}})
    out, got = run_one(svc, world, "text mom hi", ["confirm"])
    assert "Here's the iMessage to Mom (+91 98765 43210): “Hi”. Shall I send it?" in out["reply"]
    res = svc.confirm(out["actions"][0]["data"]["pending"], False, "click")
    assert res["reply"] == "Okay, not sent." and not world.sent()


def test_a_spoken_yes_must_be_the_owners_voice(world, settings):
    svc = service(world, settings, {"call dad": {"actions": [A("call.start", to="Dad")], "reply": ""}})
    out, _ = run_one(svc, world, "call dad", ["confirm"])
    pid = out["actions"][0]["data"]["pending"]
    assert not svc.confirm(pid, True, "voice", verdict="uncertain")["ok"]
    assert not world.urls
    assert svc.confirm(pid, True, "voice", verdict="verified")["ok"] and world.urls == ["tel:09822012345"]


def test_whatsapp_never_presses_return_in_another_app(world):
    world.front[0] = "Safari"
    world.macapps._open = lambda url: world.urls.append(url)  # WhatsApp never comes to the front
    assert world.macapps.whatsapp("+91 98765 43210", "hi") is False
    assert not any("key code" in s for s, _ in world.osa)


def test_whatsapp_needs_a_country_code_when_the_region_is_unknown(world):
    world.macapps._region = lambda: ""
    assert world.macapps.international("98111 22233") is None
    assert world.macapps.international("+44 20 7946 0958") == "442079460958"
    assert world.macapps.international("0044 20 7946 0958") == "442079460958"


def test_reading_messages_skips_my_own_and_reactions(world):
    texts = world.macapps.recent_texts(None, 10)
    words = [t.text for t in texts]
    assert words[0] == "Are we still on for dinner at 8?"  # from attributedBody (newer macOS)
    assert "ok" not in words and not any(w.startswith("Loved") for w in words)
    assert texts[0].sender == "Rahul Verma" and texts[1].sender == "Mom" and texts[2].sender == "Priya Sharma"
    assert abs((texts[0].when - (NOW - timedelta(minutes=10))).total_seconds()) < 61


def test_message_text_decodes_long_attributed_bodies():
    long = "x" * 300
    assert message_text(None, attributed(long)) == long
    assert message_text("plain", None) == "plain" and message_text(None, b"junk") == ""


def test_numbers_match_with_or_without_country_codes():
    assert same_number("+91 98765 43210", "098765-43210") and not same_number("12345", "12345")


def test_without_full_disk_access_messages_say_how_to_allow_it(world, settings, tmp_path):
    world.macapps.messages_db = tmp_path / "locked.db"
    world.macapps.messages_db.write_bytes(b"not a database")
    svc = service(world, settings, {"read my messages": {"actions": [A("message.read")], "reply": ""}})
    out, got = run_one(svc, world, "read my messages", ["fail"])
    assert got == ["fail"] and "Full Disk Access" in out["reply"]


def test_focus_without_the_shortcut_explains_how(world, settings):
    world.has_focus_shortcuts = False
    svc = service(world, settings, {"start focus mode": {"actions": [A("focus.start")], "reply": ""}})
    out, got = run_one(svc, world, "start focus mode", ["ok"])
    assert "JARVIS Focus On" in out["reply"] and "focus timer" in out["reply"]
    timers = [a for a in world.store.alarms(("pending",)) if a.label == "Focus"]
    assert len(timers) == 1


def test_free_slots_skip_busy_time_and_all_day_items(world, settings):
    svc = service(world, settings, {})
    d0 = datetime.combine(TOMORROW, datetime.min.time())
    world.store.add_event("Standup", d0 + timedelta(hours=10), d0 + timedelta(hours=10, minutes=30))
    world.store.add_event("Holiday", d0, d0 + timedelta(days=1))
    world.store.add_event("Lunch", d0 + timedelta(hours=13), d0 + timedelta(hours=14))
    slots, _ = svc.tools.free_slots(TOMORROW, 45, 9 * 60, 18 * 60)
    assert [(a.hour, a.minute, b.hour, b.minute) for a, b in slots] == [(9, 0, 10, 0), (10, 30, 13, 0), (14, 0, 18, 0)]


def test_web_answers_fall_back_to_a_search_without_gemini(world, settings):
    svc = service(world, settings, {"news": {"actions": [A("web.answer", question="Top news today")], "reply": ""}})
    svc.tools.ai = None
    out, got = run_one(svc, world, "news", ["ok"])
    assert "opened a web search" in out["reply"] and world.mac_calls[-1][0] == "open"


def test_screen_help_needs_gemini_and_permission(world, settings):
    svc = service(world, settings, {"what's on my screen": {"actions": [A("screen.explain")], "reply": ""}})
    world.ai.gemini_heavy = None
    out, _ = run_one(svc, world, "what's on my screen", ["fail"])
    assert "Gemini" in out["reply"] and not world.mac_calls
    world.ai.gemini_heavy = "gemini-3.5-flash-lite"
    world.mac._screen_access = lambda: False
    out, _ = run_one(svc, world, "what's on my screen", ["fail"])
    assert "Screen Recording" in out["reply"]


def test_screen_help_says_what_leaves_the_mac(world, settings):
    svc = service(world, settings, {"what's on my screen": {"actions": [A("screen.explain")], "reply": ""}})
    out, _ = run_one(svc, world, "what's on my screen", ["ok"])
    data = out["actions"][0]["data"]
    assert data["model"] == "gemini-3.5-flash-lite" and data["sent"] == "screenshot to Gemini"
    system = world.ai.calls[-1][1]
    assert "Never read out passwords" in system and "after the user confirms" in system


def test_clipboard_tools_leave_passwords_alone(world, settings):
    world.clipboard.secret = True
    svc = service(world, settings, {"translate my clipboard": {"actions": [A("clipboard.ai", task="translate")], "reply": ""}})
    out, _ = run_one(svc, world, "translate my clipboard", ["fail"])
    assert "password" in out["reply"] and not world.ai.calls


def test_files_can_only_move_within_the_allowed_folders(world, tmp_path):
    fs = FileSearch(world.db)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(FolderError):
        fs.destination(outside)
    with pytest.raises(FolderError):
        fs.move(world.home / "Downloads" / "Lease Agreement.txt", folder=outside)
    (world.home / "Documents" / "Lease Agreement.txt").write_text("other")
    with pytest.raises(FolderError):  # never replaces a file
        fs.move(world.home / "Downloads" / "Lease Agreement.txt", folder=world.home / "Documents")
    assert fs.new_name(Path("a/report.pdf"), "tax return 2026") == "tax return 2026.pdf"
    assert fs.new_name(Path("a/report.pdf"), "final.pdf") == "final.pdf"
    with pytest.raises(FolderError):
        fs.new_name(Path("a/report.pdf"), " .. ")


def test_reading_files(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("# Title\n\nSome words")
    assert read_text(p) == "# Title\nSome words"
    with pytest.raises(ReadError):
        read_text(tmp_path / "x.zip")
    (tmp_path / "e.txt").write_text("   \n")
    with pytest.raises(ReadError):
        read_text(tmp_path / "e.txt")
    doc = tmp_path / "w.docx"
    doc.write_bytes(b"PK")
    assert read_text(doc, textutil=lambda path: "From Word") == "From Word"


def test_reading_a_real_pdf(tmp_path):
    p = tmp_path / "hello.pdf"
    p.write_bytes(minimal_pdf("Notice period two months"))
    assert "Notice period two months" in read_text(p)


def minimal_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def test_weather_is_spoken_plainly():
    w = Weather(transport=weather_transport())
    from jarvis.tools.weather import spoken

    f = w.forecast("Pune")
    assert spoken(f, False) == ("In Pune it's 27 degrees and partly cloudy, feels like 30. "
                                "Today: a high of 31 and a low of 22, 40% chance of rain.")
    assert spoken(w.forecast("Pune", tomorrow=True), False, tomorrow=True) == (
        "Tomorrow in Pune: rain, a high of 29 and a low of 22, 80% chance of rain.")
    assert "डिग्री" in spoken(f, True)


def test_the_weather_host_is_allowed_only_with_the_online_brain():
    from jarvis.tools.weather import HOSTS

    assert HOSTS == ("open-meteo.com",)


def test_the_command_prompt_lists_the_everyday_tools_and_rules():
    from jarvis.llm.intents import system_prompt

    p = system_prompt("Jarvis", "Aditya")
    for t in NEW_TOOLS:
        assert f"- {t}:" in p
    assert "screen.explain" in p and "web.answer" in p and "say you can't look that up" not in p


def test_english_with_two_thes_stays_english():
    assert detect_language("move the file to the desktop") == "en"
    assert detect_language("fix the grammar of the text I copied") == "en"
    assert detect_language("kal shaam 5 baje meeting rakh do") == "hinglish"


def test_gemini_ask_sends_the_image_and_the_search_tool():
    import base64

    from jarvis.llm.gemini import GeminiClient, QuotaExceeded

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if "quota" in json.dumps(body):
            return httpx.Response(429, json={"error": {"message": "per day limit"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "thinking…", "thought": True}, {"text": " It's 27 degrees. "}]}}]})

    c = GeminiClient(lambda: "k", transport=httpx.MockTransport(handler))
    assert c.ask("gemini-3.5-flash-lite", "sys", "what's on screen", image=(b"\xff\xd8x", "image/jpeg")) == "It's 27 degrees."
    path, body = seen[-1]
    assert path.endswith("gemini-3.5-flash-lite:generateContent") and "tools" not in body
    inline = body["contents"][0]["parts"][0]["inline_data"]
    assert inline["mime_type"] == "image/jpeg" and base64.b64decode(inline["data"]) == b"\xff\xd8x"
    c.ask("m", "sys", "news", search=True)
    assert seen[-1][1]["tools"] == [{"google_search": {}}]
    with pytest.raises(QuotaExceeded):
        c.ask("m", "sys", "quota")


def test_looking_things_up_needs_the_gemini_brain(settings, world):
    from jarvis.llm.client import LLMUnavailable

    b = Brain(settings, world.db, names=lambda: ("J", "A"), voice_gender=lambda: "female", server=FakeOllamaServer(),
              client=FakeOllama())
    with pytest.raises(LLMUnavailable):
        b.look_up("sys", "news")
    with pytest.raises(LLMUnavailable):
        b.see("sys", "screen", b"x")


def test_search_and_screen_use_gemini_whenever_a_key_is_saved(settings, world):
    from jarvis.llm.gemini import HEAVY_MODEL, GeminiClient

    key = ["k"]
    b = Brain(settings, world.db, names=lambda: ("J", "A"), voice_gender=lambda: "female", server=FakeOllamaServer(),
              client=FakeOllama(), cloud=GeminiClient(lambda: key[0]))
    world.db.set("ai.provider", "gemini")
    assert b.gemini_heavy == HEAVY_MODEL
    world.db.set("ai.heavy_model", "gemini-3.8-flash")
    assert b.gemini_heavy == "gemini-3.8-flash"
    key[0] = ""
    assert b.gemini_heavy is None
    key[0] = "k"
    world.db.set("ai.provider", "ollama")
    assert b.gemini_heavy is None  # the owner chose to keep everything on the Mac
