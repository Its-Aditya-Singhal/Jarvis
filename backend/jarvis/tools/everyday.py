"""Everyday tools, mixed into the ToolRunner: messages and calls, Apple Reminders, weather and
web answers, reading / moving / renaming files, free time and meeting invitations, focus mode,
music, the clipboard through the AI, the daily briefing, and screen help.

The same rules as every other tool: arguments from the model are checked here, spoken replies
describe what actually happened, and anything that reaches other people (a message, a call, a
meeting invitation) or changes a file (move, rename) is a level-3 plan: it is read back and runs
only after the owner's voice-verified "yes" or a click on Confirm.

What leaves the Mac, and only on request: the question for a web answer, a file's text for a
summary, the clipboard's text for the clipboard tools, and a screenshot for screen help (all to
the Gemini API, like any other request), and the city name for the weather (Open-Meteo).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from ..google.auth import GoogleError
from ..google.gmail import EMAIL_RE
from ..llm.client import LLMUnavailable
from ..llm.gemini import QuotaExceeded
from ..llm.intents import clock_phrase, day_phrase, parse_local, wait_phrase
from .apple import AppleError
from .files import FolderError
from .macapps import DISTRACTING, FOCUS_ON, AppError, Contact, MacApps, Text, digits
from .reading import ReadError, read_text
from .results import Plan, ToolResult
from .weather import Weather, WeatherError, spoken

if TYPE_CHECKING:
    from ..database.db import Database
    from .apple import AppleBridge
    from .apps import AppIndex
    from .files import FileSearch
    from .google import GoogleTools
    from .mac import MacControl
    from .store import Event, ToolStore

log = logging.getLogger(__name__)

HOME_CITY = "home_city"
PRONOUNS = {"him", "her", "them", "he", "she", "they", "usko", "use", "unko", "unhe", "isko", "back", "sender",
            "that person", "the sender"}
PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,20}$")
MAX_TEXT = 1000  # characters in one message
FOCUS_LABEL = "Focus"
# what screen help may offer: the things JARVIS can actually do
CAN_DO = ("open apps, websites and System Settings pages; look things up on the web; find, summarise, move or rename "
          "files; read and send email, iMessages and WhatsApp messages (after the user confirms); add reminders, notes "
          "and calendar events; type text into the app in front; translate, summarise or fix copied text; control "
          "music, volume and brightness; take screenshots; and do simple things in Mac apps with AppleScript")


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _q(s: str, n: int = 80) -> str:
    return f"“{_clip(s, n)}”"


def _and(items: list[str], hi: bool) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + (" और " if hi else " and ") + items[-1]


def _end(s: str, hi: bool) -> str:
    s = s.strip()
    return s if not s or s[-1] in ".!?।" else s + ("।" if hi else ".")


class Everyday:
    """Mixin for ToolRunner (which provides db, store, mac, files, apple, google, clock, ...)."""

    # set by ToolRunner.__init__
    macapps: MacApps
    weather: Weather
    ai: Any  # the Brain: write / look_up / see (None until the language model is on)
    names: Callable[[], tuple[str, str]]
    last_text: Text | None
    db: Database
    store: ToolStore
    mac: MacControl
    files: FileSearch
    apps: AppIndex
    apple: AppleBridge  # None in some tests: checked before use
    google: GoogleTools | None
    clock: Callable[[], datetime]
    on_change: Callable[[], None]
    last_files: list[Path]

    if TYPE_CHECKING:  # ToolRunner's own tools these build on
        def _one_file(self, tool: str, args: dict, hi: bool) -> Path | ToolResult: ...
        def _web_open(self, args: dict, hi: bool) -> ToolResult: ...
        def _media_control(self, args: dict, hi: bool) -> ToolResult: ...
        def _clipboard_text(self, tool: str, hi: bool) -> str | ToolResult: ...
        def events_on(self, day: date) -> tuple[list[Event], str | None]: ...

    # -- shared ----------------------------------------------------------------------------------
    def _fail(self, tool: str, en: str, hin: str, hi: bool, **data) -> ToolResult:
        return ToolResult(tool, False, hin if hi else en, data)

    def _ok(self, tool: str, en: str, hin: str, hi: bool, **data) -> ToolResult:
        return ToolResult(tool, True, hin if hi else en, data)

    def _lang_rule(self, hi: bool) -> str:
        return "Answer in Hindi (Devanagari script)." if hi else "Answer in English."

    def _spoken_rule(self, n: int) -> str:
        return f"It is spoken aloud: at most {n} short sentences, no lists, markdown, links or emoji."

    # -- people -----------------------------------------------------------------------------------
    def _person(self, tool: str, to: str, hi: bool, phone: bool) -> tuple[str, str] | ToolResult:
        """(name, phone number or address) for a contact name, a number, or "him" (the person whose
        message was just read). ``phone``: a phone number is needed (WhatsApp, a phone call)."""
        to = " ".join(str(to or "").split())[:80]
        last = self.last_text
        if (not to or to.lower() in PRONOUNS) and last is not None:
            return last.sender, last.handle
        if not to:
            return self._fail(tool, "Who to?", "किसे?", hi)
        if PHONE_RE.match(to) and len(digits(to)) >= 7:
            return self.macapps.name_for(to), to
        if EMAIL_RE.match(to):
            if phone:
                return self._fail(tool, "I need a phone number for that, not an email address.",
                                  "इसके लिए फ़ोन नंबर चाहिए।", hi)
            return self.macapps.name_for(to), to.lower()
        try:
            found = self.macapps.find_contact(to)
        except AppError as exc:
            return self._fail(tool, _end(str(exc)[:1].upper() + str(exc)[1:], False), "कॉन्टैक्ट्स नहीं पढ़ पाई।", hi)
        if not found:
            return self._fail(tool, f"I couldn't find {to} in your contacts.", f"कॉन्टैक्ट्स में {to} नहीं मिला।", hi, to=to)
        c: Contact = found[0]
        if c.phones:
            return c.name, c.phones[0]
        if c.emails and not phone:
            return c.name, c.emails[0]
        return self._fail(tool, f"{c.name} has no phone number in your contacts.", f"{c.name} का फ़ोन नंबर नहीं है।", hi)

    # -- messages ----------------------------------------------------------------------------------
    def _plan_message_send(self, args: dict, hi: bool) -> Plan | ToolResult:
        text = " ".join(str(args.get("text") or args.get("message") or args.get("about") or "").split())[:MAX_TEXT]
        if not text:
            return self._fail("message.send", "What should the message say?", "मैसेज में क्या लिखूँ?", hi)
        app = "whatsapp" if "whats" in str(args.get("app") or "").lower() else "imessage"
        who = self._person("message.send", str(args.get("to") or ""), hi, phone=app == "whatsapp")
        if not isinstance(who, tuple):
            return who
        name, handle = who
        label = "WhatsApp message" if app == "whatsapp" else "iMessage"
        if app == "whatsapp":  # read back the full number WhatsApp will use (a saved 0-prefixed one gets the country code)
            intl = self.macapps.international(handle)
            if intl is None:
                return self._fail("message.send", f"I need {name}'s number with its country code for WhatsApp; save it like "
                                  "+91 98765 43210 in Contacts.", f"WhatsApp के लिए {name} का नंबर देश के कोड के साथ चाहिए।", hi)
            handle = "+" + intl
        to = name if name == handle else f"{name} ({handle})"
        ask = (f"{to} को {'WhatsApp पर ' if app == 'whatsapp' else ''}यह मैसेज भेजूँ: “{text}”? “हाँ, भेज दो” कहिए या Confirm दबाइए।" if hi
               else f"Here's the {label} to {to}: “{text}”. Shall I send it? Say “yes, send it” or click Confirm.")
        return Plan("message.send", 0, f"a {label} to {name}", hi, ask=ask, detail=f"To: {name} <{handle}>\n\n{text}",
                    ref=(app, name, handle, text), cancel=("ठीक है, नहीं भेजा।" if hi else "Okay, not sent."))

    def _do_message_send(self, plan: Plan) -> ToolResult:
        hi = plan.hi
        app, name, handle, text = plan.ref
        try:
            if app == "whatsapp":
                if self.macapps.whatsapp(handle, text):
                    return self._ok("message.send", f"Sent your WhatsApp message to {name}.",
                                    f"{name} को WhatsApp मैसेज भेज दिया।", hi, to=handle, app=app)
                return self._ok("message.send", f"WhatsApp is open with your message to {name}; press Return to send it.",
                                f"WhatsApp में {name} के लिए मैसेज लिखा है, भेजने के लिए Return दबाइए।", hi, to=handle,
                                app=app, sent=False)
            service = self.macapps.send_imessage(handle, text)
        except AppError as exc:
            return self._fail("message.send", _end(f"I couldn't send it: {exc}", False), "मैसेज नहीं भेज पाई।", hi)
        return self._ok("message.send", f"Sent your {'text' if service == 'SMS' else 'iMessage'} to {name}.",
                        f"{name} को मैसेज भेज दिया।", hi, to=handle, app=app, service=service)

    def _message_read(self, args: dict, hi: bool) -> ToolResult:
        who = " ".join(str(args.get("from") or "").split())[:80]
        try:
            n = max(1, min(int(float(args.get("count") or 5)), 10))
        except (TypeError, ValueError):
            n = 5
        contact = None
        try:
            if who and who.lower() not in PRONOUNS:
                found = self.macapps.find_contact(who)
                if not found:
                    return self._fail("message.read", f"I couldn't find {who} in your contacts.",
                                      f"कॉन्टैक्ट्स में {who} नहीं मिला।", hi)
                contact = found[0]
            elif who and self.last_text is not None:
                contact = Contact(self.last_text.sender, [self.last_text.handle] if "@" not in self.last_text.handle else [],
                                  [self.last_text.handle] if "@" in self.last_text.handle else [])
            texts = self.macapps.recent_texts(contact, n, None if contact else self.clock() - timedelta(days=2))
        except AppError as exc:
            return self._fail("message.read", _end(str(exc)[:1].upper() + str(exc)[1:], False),
                              "मैसेज नहीं पढ़ पाई (Full Disk Access चाहिए)।", hi, permission="full_disk_access")
        data = {"messages": [{"from": t.sender, "text": t.text, "time": t.when.isoformat(timespec="minutes"),
                              "unread": t.unread} for t in texts]}
        if not texts:
            return self._ok("message.read", f"No messages from {contact.name} yet." if contact else "No new messages in the last two days.",
                            f"{contact.name} का कोई मैसेज नहीं है।" if contact else "कोई नया मैसेज नहीं है।", hi, **data)
        self.last_text = texts[0]
        now = self.clock()
        if not contact:
            unread = [t for t in texts if t.unread]
            texts = unread or texts[:3]
        parts = []
        for t in texts[:5]:
            when = clock_phrase(t.when, hi) if t.when.date() == now.date() else day_phrase(t.when.date(), now.date(), hi)
            parts.append(f"{when}: {_q(t.text, 160)}" if contact else f"{t.sender}, {when}: {_q(t.text, 160)}")
        if hi:
            say = ("नए मैसेज: " if not contact else f"{contact.name} के मैसेज: ") + "; ".join(parts) + "।"
        elif contact:
            say = f"The latest from {contact.name}: " + "; ".join(parts) + "."
        else:
            say = (f"You have {len(texts)} unread message{'s' if len(texts) != 1 else ''}. " if any(t.unread for t in texts)
                   else "No unread messages. The latest: ") + "; ".join(parts) + "."
        return self._ok("message.read", say, say, hi, **data)

    def _plan_call_start(self, args: dict, hi: bool) -> Plan | ToolResult:
        video = str(args.get("video")).lower() in ("true", "1", "yes", "on")
        who = self._person("call.start", str(args.get("to") or ""), hi, phone=not video)
        if not isinstance(who, tuple):
            return who
        name, handle = who
        kind = "FaceTime video" if video else "call"
        to = name if name == handle else f"{name} ({handle})"
        ask = (f"{to} को {'FaceTime वीडियो ' if video else ''}कॉल करूँ? “हाँ, कर दो” कहिए या Confirm दबाइए।" if hi
               else f"{'FaceTime' if video else 'Call'} {to}? Say “yes, go ahead” or click Confirm.")
        return Plan("call.start", 0, f"a {kind} to {name}", hi, ask=ask, detail=f"{kind}: {name} <{handle}>",
                    ref=(name, handle, video), cancel=("ठीक है, कॉल नहीं किया।" if hi else "Okay, I won't call."))

    def _do_call_start(self, plan: Plan) -> ToolResult:
        name, handle, video = plan.ref
        try:
            self.macapps.call(handle, video)
        except AppError as exc:
            return self._fail("call.start", _end(f"I couldn't start the call: {exc}", False), "कॉल नहीं हो पाई।", plan.hi)
        return self._ok("call.start", f"Calling {name}" + (" on FaceTime." if video else "; macOS may ask you to press Call."),
                        f"{name} को कॉल कर रही हूँ।", plan.hi, to=handle)

    # -- reminders ---------------------------------------------------------------------------------
    def _reminder_add(self, args: dict, hi: bool) -> ToolResult:
        text = " ".join(str(args.get("text") or args.get("title") or "").split())[:300]
        if not text:
            return self._fail("reminder.add", "What should the reminder say?", "रिमाइंडर में क्या लिखूँ?", hi)
        if self.apple is None:
            return self._fail("reminder.add", "Apple Reminders isn't available here.", "Apple Reminders उपलब्ध नहीं है।", hi)
        now = self.clock()
        due = parse_local(args.get("due") or args.get("time"), need_time=True)
        if due is not None and due <= now:
            return self._fail("reminder.add", "That time has already passed.", "वह समय निकल चुका है।", hi)
        try:
            used = self.apple.add_reminder(text, due, " ".join(str(args.get("list") or "").split())[:60])
        except AppleError as exc:
            return self._fail("reminder.add", _end(f"I couldn't add it to Reminders: {exc}", False),
                              "Reminders में नहीं जोड़ पाई।", hi)
        when_en = f" for {day_phrase(due.date(), now.date(), False)} at {clock_phrase(due, False)}" if due else ""
        when_hi = f" {day_phrase(due.date(), now.date(), True)} {clock_phrase(due, True)} के लिए" if due else ""
        where = f"your {used} list" if used and used.lower() != "reminders" else "your Reminders"
        return self._ok("reminder.add", f"Added {_q(text)} to {where}{when_en}.",
                        f"{_q(text)}{when_hi} Reminders में जोड़ दिया है।", hi, list=used,
                        due=due.isoformat(timespec="minutes") if due else None)

    def _reminder_list(self, args: dict, hi: bool) -> ToolResult:
        if self.apple is None:
            return self._fail("reminder.list", "Apple Reminders isn't available here.", "Apple Reminders उपलब्ध नहीं है।", hi)
        try:
            items = self.apple.reminders()
        except AppleError as exc:
            return self._fail("reminder.list", _end(f"I couldn't read Reminders: {exc}", False), "Reminders नहीं पढ़ पाई।", hi)
        now = self.clock()
        today_only = str(args.get("when") or "today").lower() not in ("all", "everything", "any")
        end = datetime.combine(now.date(), datetime.max.time())
        shown = [(t, d) for t, d in items if d is not None and d <= end] if today_only else items
        data = {"reminders": [{"title": t, "due": d.isoformat(timespec="minutes") if d else None} for t, d in shown]}
        if not shown:
            return self._ok("reminder.list", "Nothing due today in your Reminders." if today_only else "Your Reminders list is empty.",
                            "आज कोई रिमाइंडर नहीं है।" if today_only else "कोई रिमाइंडर नहीं है।", hi, **data)
        def one(t: str, d: datetime | None) -> str:
            if d is None:
                return t
            if d < now:
                return f"{t} ({'बाकी' if hi else 'overdue'})"
            return f"{t} {clock_phrase(d, True)}" if hi else f"{t} at {clock_phrase(d, False)}" if d.date() == now.date() \
                else f"{t} {day_phrase(d.date(), now.date(), False)}"
        listed = _and([one(t, d) for t, d in shown[:5]], hi)
        more = len(shown) - min(5, len(shown))
        return self._ok("reminder.list", f"You have {len(shown)} reminder{'s' if len(shown) != 1 else ''}"
                        f"{' for today' if today_only else ''}: {listed}" + (f", and {more} more." if more else "."),
                        f"{len(shown)} रिमाइंडर: {listed}।", hi, **data)

    # -- weather and the web ----------------------------------------------------------------------------
    @property
    def home_city(self) -> str:
        return self.db.get(HOME_CITY) or ""

    def _weather(self, args: dict, hi: bool) -> ToolResult:
        place = " ".join(str(args.get("place") or args.get("city") or "").split())[:80] or self.home_city
        if not place:
            return self._fail("weather", "Which city? Tell me “my city is …” once and I'll remember it for the weather.",
                              "कौन सा शहर? एक बार “मेरा शहर … है” कहिए, मैं याद रखूँगी।", hi, need="city")
        tomorrow = str(args.get("day") or "").lower() in ("tomorrow", "kal")
        try:
            f = self.weather.forecast(place, tomorrow)
        except (WeatherError, OSError) as exc:
            return self._fail("weather", _end(str(exc)[:1].upper() + str(exc)[1:], False), "मौसम की जानकारी नहीं मिल पाई।", hi)
        return self._ok("weather", spoken(f, hi, tomorrow), spoken(f, hi, tomorrow), hi, place=f.place,
                        now_c=f.now_c, high_c=f.high_c, low_c=f.low_c, rain_pct=f.rain_pct)

    def _location_set(self, args: dict, hi: bool) -> ToolResult:
        city = " ".join(str(args.get("city") or args.get("place") or "").split())[:80]
        if not city:
            return self._fail("location.set", "Which city?", "कौन सा शहर?", hi)
        try:
            city = self.weather.place(city).name
        except (WeatherError, OSError) as exc:
            if "don't know" in str(exc):
                return self._fail("location.set", _end(str(exc), False), f"{city} नाम की जगह नहीं मिली।", hi)
        self.db.set(HOME_CITY, city)
        return self._ok("location.set", f"Got it: your city is {city}. I'll use it for the weather and your briefing.",
                        f"ठीक है, आपका शहर {city} है। मौसम और ब्रीफ़िंग के लिए याद रखूँगी।", hi, city=city)

    def _web_answer(self, args: dict, hi: bool) -> ToolResult:
        q = " ".join(str(args.get("question") or args.get("query") or "").split())[:500]
        if not q:
            return self._fail("web.answer", "What should I look up?", "क्या ढूँढूँ?", hi)
        assistant, owner = self.names()
        now = self.clock()
        city = self.home_city
        system = (f"You are {assistant}, {owner}'s voice assistant. Search the web for current information and answer the "
                  f"question directly. {self._spoken_rule(3)} Today is {now.strftime('%A %d %B %Y, %H:%M')}"
                  + (f"; {owner} lives in {city}" if city else "") + ". If you can't find it, say so briefly. "
                  + self._lang_rule(hi))
        try:
            if self.ai is None:
                raise LLMUnavailable("no AI")
            text = self.ai.look_up(system, q)
        except LLMUnavailable as exc:
            log.info("web answer unavailable (%s): opening a search", exc)
            res = self._web_open({"target": q}, hi)
            why = "my free web lookups are used up for now" if isinstance(exc, QuotaExceeded) else "I can't look that up myself right now"
            return self._ok("web.answer", f"{why[:1].upper() + why[1:]}, so I opened a web search for it.",
                            "अभी खुद नहीं ढूँढ पाई, इसलिए वेब पर खोज खोल दी है।", hi, url=res.data.get("url"), fallback=True)
        if not text:
            return self._fail("web.answer", "I couldn't find an answer.", "जवाब नहीं मिला।", hi)
        return self._ok("web.answer", _end(text, hi), _end(text, hi), hi, question=q)

    # -- files -------------------------------------------------------------------------------------------
    def _files_summarize(self, args: dict, hi: bool) -> ToolResult:
        p = self._one_file("files.summarize", args, hi)
        if not isinstance(p, Path):
            return p
        try:
            p = self.files.movable(p)
            text = read_text(p)
        except (FolderError, ReadError) as exc:
            return self._fail("files.summarize", _end(f"I can't read {_q(p.name)}: {exc}", False),
                              f"{_q(p.name)} नहीं पढ़ पाई।", hi, files=[str(p)])
        if self.ai is None:
            return self._fail("files.summarize", "I need the AI to summarise it.", "सारांश के लिए AI चाहिए।", hi)
        assistant, owner = self.names()
        question = " ".join(str(args.get("question") or "").split())[:300]
        task = (f"Answer {owner}'s question about this document: {question}" if question
                else f"Summarise this document for {owner}: what it is and the points that matter")
        try:
            out = self.ai.write(f"You are {assistant}, {owner}'s voice assistant. {task}. {self._spoken_rule(5)} "
                                f"{self._lang_rule(hi)}", f"File: {p.name}\n\n{text}", 450)
        except (LLMUnavailable, ValueError) as exc:
            return self._fail("files.summarize", _end(f"I can't summarise right now: {exc}", False),
                              "अभी सारांश नहीं बना पाई।", hi)
        self.last_files = [p]
        return self._ok("files.summarize", _end(out, hi), _end(out, hi), hi, files=[str(p)])

    def _plan_files_move(self, args: dict, hi: bool) -> Plan | ToolResult:
        to = " ".join(str(args.get("to") or args.get("destination") or "").split())[:80]
        if not to:
            return self._fail("files.move", "Which folder should it go to?", "किस फ़ोल्डर में?", hi)
        dest = self.mac.folder(to)
        if dest is None:
            return self._fail("files.move", f"I don't know a folder called {_q(to)}.", f"{_q(to)} फ़ोल्डर नहीं मिला।", hi)
        p = self._one_file("files.move", {k: v for k, v in args.items() if k not in ("to", "destination")}, hi)
        if not isinstance(p, Path):
            return p
        try:
            p = self.files.movable(p)
            dest = self.files.destination(dest)
        except FolderError as exc:
            return self._fail("files.move", _end(f"I can't move {_q(p.name)}: {exc}", False), f"{_q(p.name)} नहीं हिला सकती।", hi)
        if dest == p.parent:
            return self._fail("files.move", f"{_q(p.name)} is already in {dest.name}.", f"{_q(p.name)} पहले से {dest.name} में है।", hi)
        ask = (f"{p.parent.name} से {_q(p.name)} को {dest.name} में ले जाऊँ? “हाँ, कर दो” कहिए या Confirm दबाइए।" if hi
               else f"Move {_q(p.name)} from {p.parent.name} to {dest.name}? Say “yes, go ahead” or click Confirm.")
        return Plan("files.move", 0, f"{_q(p.name)} to {dest.name}", hi, path=str(p), ask=ask, detail=f"{p}\n→ {dest}/",
                    ref=dest, cancel=("ठीक है, नहीं हिलाया।" if hi else "Okay, I left it where it is."))

    def _plan_files_rename(self, args: dict, hi: bool) -> Plan | ToolResult:
        spoken_name = " ".join(str(args.get("name") or args.get("to") or "").split())[:200]
        p = self._one_file("files.rename", {k: v for k, v in args.items() if k not in ("name", "to")}, hi)
        if not isinstance(p, Path):
            return p
        try:
            p = self.files.movable(p)
            new = self.files.new_name(p, spoken_name)
        except FolderError as exc:
            return self._fail("files.rename", _end(f"I can't rename {_q(p.name)}: {exc}", False), f"{_q(p.name)} का नाम नहीं बदल सकती।", hi)
        if (p.parent / new).exists():
            return self._fail("files.rename", f"There's already a file called {_q(new)} there.", f"{_q(new)} नाम की फ़ाइल पहले से है।", hi)
        ask = (f"{_q(p.name)} का नाम {_q(new)} कर दूँ? “हाँ, कर दो” कहिए या Confirm दबाइए।" if hi
               else f"Rename {_q(p.name)} to {_q(new)}? Say “yes, go ahead” or click Confirm.")
        return Plan("files.rename", 0, f"{_q(p.name)} → {_q(new)}", hi, path=str(p), ask=ask, detail=f"{p}\n→ {new}",
                    ref=new, cancel=("ठीक है, नाम नहीं बदला।" if hi else "Okay, I kept the name."))

    def _do_files_move(self, plan: Plan) -> ToolResult:
        return self._moved(plan, folder=plan.ref)

    def _do_files_rename(self, plan: Plan) -> ToolResult:
        return self._moved(plan, name=plan.ref)

    def _moved(self, plan: Plan, folder: Path | None = None, name: str | None = None) -> ToolResult:
        hi, tool = plan.hi, plan.tool
        try:
            p = self.files.move(plan.path, folder=folder, name=name)
        except (FolderError, OSError) as exc:
            return self._fail(tool, _end(f"That didn't work: {exc}", False), "नहीं हो पाया।", hi)
        self.last_files = [p]
        if folder is not None:
            return self._ok(tool, f"Moved {_q(p.name)} to {p.parent.name}.", f"{_q(p.name)} {p.parent.name} में रख दी है।", hi, path=str(p))
        return self._ok(tool, f"Renamed it to {_q(p.name)}.", f"नाम {_q(p.name)} कर दिया है।", hi, path=str(p))

    # -- calendar helpers -----------------------------------------------------------------------------
    @staticmethod
    def _hhmm(v: Any, default: int) -> int:
        m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*", str(v or ""))
        if not m:
            return default
        mins = int(m.group(1)) * 60 + int(m.group(2) or 0)
        return mins if 0 <= mins <= 24 * 60 else default

    def free_slots(self, day: date, minutes: int = 30, after: int = 9 * 60, before: int = 18 * 60
                   ) -> tuple[list[tuple[datetime, datetime]], str | None]:
        """Gaps of at least ``minutes`` between ``after`` and ``before`` (minutes past midnight)."""
        events, err = self.events_on(day)
        d0 = datetime.combine(day, datetime.min.time())
        start, stop = d0 + timedelta(minutes=after), d0 + timedelta(minutes=before)
        now = self.clock()
        if day == now.date():
            soon = now.replace(second=0, microsecond=0)
            soon += timedelta(minutes=(-soon.minute) % 15)
            start = max(start, soon)
        busy = []
        for e in events:
            end = e.end if e.end and e.end > e.start else e.start + timedelta(hours=1)  # Apple events carry no end
            if end - e.start >= timedelta(hours=23):
                continue  # all-day items (a birthday, a holiday) don't fill the day
            busy.append((e.start, end))
        slots, t = [], start
        for a, b in sorted(busy):
            if a > t and a - t >= timedelta(minutes=minutes) and t < stop:
                slots.append((t, min(a, stop)))
            t = max(t, b)
        if stop > t and stop - t >= timedelta(minutes=minutes):
            slots.append((t, stop))
        return [s for s in slots if s[1] - s[0] >= timedelta(minutes=minutes)], err

    def _calendar_free(self, args: dict, hi: bool) -> ToolResult:
        now = self.clock()
        d = parse_local(f"{args.get('date') or now.date().isoformat()}T00:00")
        day = d.date() if d else now.date()
        try:
            minutes = max(5, min(int(float(args.get("minutes") or 30)), 8 * 60))
        except (TypeError, ValueError):
            minutes = 30
        after, before = self._hhmm(args.get("after"), 9 * 60), self._hhmm(args.get("before"), 18 * 60)
        if before <= after:
            after, before = 9 * 60, 18 * 60
        slots, err = self.free_slots(day, minutes, after, before)
        dp = day_phrase(day, now.date(), hi)
        data = {"date": day.isoformat(), "minutes": minutes,
                "slots": [{"start": a.isoformat(timespec="minutes"), "end": b.isoformat(timespec="minutes")} for a, b in slots]}
        if not slots:
            say = (f"{dp} {wait_phrase(minutes * 60, True)} का कोई खाली समय नहीं है।" if hi else
                   f"You have no free {wait_phrase(minutes * 60, False)} {dp} between {clock_phrase(datetime.combine(day, datetime.min.time()) + timedelta(minutes=after), False)} and {clock_phrase(datetime.combine(day, datetime.min.time()) + timedelta(minutes=before), False)}.")
        else:
            spans = [f"{clock_phrase(a, hi)} से {clock_phrase(b, hi)}" if hi else f"{clock_phrase(a, False)} to {clock_phrase(b, False)}"
                     for a, b in slots[:3]]
            say = (f"{dp} आप खाली हैं: {_and(spans, True)}।" if hi else
                   f"You're free {dp} {_and(spans, False)}" + (f", and {len(slots) - 3} more gaps." if len(slots) > 3 else "."))
        if err:
            say += "" if hi else f" (Couldn't read everything: {err}.)"
        return self._ok("calendar.free", say, say, hi, **data)

    def _plan_calendar_invite(self, args: dict, hi: bool) -> Plan | ToolResult:
        if not getattr(self, "google_calendar", False) or self.google is None:
            return self._fail("calendar.invite", "Inviting people needs Google Calendar: connect it in Settings → Google account.",
                              "लोगों को बुलाने के लिए Google Calendar जोड़िए।", hi)
        now = self.clock()
        start = parse_local(args.get("start"), need_time=True)
        title = " ".join(str(args.get("title") or "").split())[:120] or "Meeting"
        if start is None:
            return self._fail("calendar.invite", "When should the meeting be?", "मीटिंग कब रखूँ?", hi)
        if start <= now:
            return self._fail("calendar.invite", "That time has already passed.", "वह समय निकल चुका है।", hi)
        end = parse_local(args.get("end"), need_time=True)
        if end is None or end <= start:
            end = start + timedelta(minutes=30 if re.search(r"\b(call|sync|chat|catch ?up)\b", title.lower()) else 60)
        raw = args.get("with") or args.get("attendees") or ""
        names = raw if isinstance(raw, list) else re.split(r",|\band\b|\baur\b|&", str(raw))
        names = [" ".join(str(n).split()) for n in names if str(n).strip()][:10]
        if not names:
            return self._fail("calendar.invite", "Who should I invite?", "किसे बुलाऊँ?", hi)
        people = []
        for n in names:
            try:
                found = self.google.gmail.find_address(n)
            except GoogleError as exc:
                return self._fail("calendar.invite", _end(str(exc), False), "Gmail से पता नहीं मिल पाया।", hi)
            if found is None:
                return self._fail("calendar.invite", f"I couldn't find an email address for {n}. Say their address, or mail them once.",
                                  f"{n} का ईमेल पता नहीं मिला।", hi, who=n)
            people.append(found)
        who_en = _and([nm if nm == a else f"{nm} ({a})" for nm, a in people], False)
        who_hi = _and([nm for nm, _ in people], True)
        when_en = f"{day_phrase(start.date(), now.date(), False)} at {clock_phrase(start, False)} to {clock_phrase(end, False)}"
        when_hi = f"{day_phrase(start.date(), now.date(), True)} {clock_phrase(start, True)}"
        ask = (f"{when_hi} {_q(title)} रखूँ और {who_hi} को बुलाऊँ? Google उन्हें ईमेल से न्योता भेजेगा। “हाँ, कर दो” कहिए या Confirm दबाइए।" if hi
               else f"Set up {_q(title)} {when_en} and invite {who_en}? Google will email them the invitation. "
                    "Say “yes, go ahead” or click Confirm.")
        detail = f"{title}\n{start:%a %d %b %H:%M} – {end:%H:%M}\nInvite: " + ", ".join(f"{nm} <{a}>" for nm, a in people)
        return Plan("calendar.invite", 0, f"{_q(title)} with {who_en}", hi, ask=ask, detail=detail,
                    ref=(title, start, end, people), cancel=("ठीक है, मीटिंग नहीं रखी।" if hi else "Okay, no meeting set up."))

    def _do_calendar_invite(self, plan: Plan) -> ToolResult:
        title, start, end, people = plan.ref
        hi = plan.hi
        assert self.google is not None
        try:
            self.google.calendar.create(title, start, end, [a for _, a in people])
        except GoogleError as exc:
            return self._fail("calendar.invite", _end(f"Google Calendar refused it: {exc}", False), "मीटिंग नहीं बन पाई।", hi)
        self.store.add_event(title, start, end)
        self.on_change()
        now = self.clock()
        who = _and([nm for nm, _ in people], hi)
        return self._ok("calendar.invite", f"Done: {_q(title)} is on your calendar {day_phrase(start.date(), now.date(), False)} "
                        f"at {clock_phrase(start, False)}, and {who} {'has' if len(people) == 1 else 'have'} been invited.",
                        f"{_q(title)} कैलेंडर में है और {who} को न्योता भेज दिया है।", hi,
                        start=start.isoformat(timespec="minutes"), invited=[a for _, a in people])

    # -- focus ----------------------------------------------------------------------------------------
    def _focus_start(self, args: dict, hi: bool) -> ToolResult:
        if str(args.get("dnd_only")).lower() in ("true", "1", "yes"):  # just "turn on Do Not Disturb"
            try:
                on = self.macapps.set_focus(True)
            except AppError as exc:
                return self._fail("focus.start", _end(f"Do Not Disturb didn't turn on: {exc}", False),
                                  "Do Not Disturb चालू नहीं हो पाया।", hi)
            if not on:
                return self._fail("focus.start", f"macOS doesn't let apps switch Do Not Disturb directly. Make a shortcut named "
                                  f"“{FOCUS_ON}” once (see the user guide) and I'll use it.",
                                  f"Do Not Disturb के लिए “{FOCUS_ON}” नाम का शॉर्टकट बनाइए।", hi, need="shortcut")
            return self._ok("focus.start", "Do Not Disturb is on.", "Do Not Disturb चालू है।", hi, dnd=True)
        try:
            minutes = max(5, min(int(float(args.get("minutes") or 25)), 240))
        except (TypeError, ValueError):
            minutes = 25
        extra = args.get("close") or []
        extra = extra if isinstance(extra, list) else re.split(r",|\band\b", str(extra))
        wanted = {n.lower(): n for n in DISTRACTING}
        wanted.update({str(n).strip().lower(): str(n).strip() for n in extra if str(n).strip()})
        closed = []
        for app in self.mac.running():
            if app.lower() in wanted and app.lower() not in ("jarvis", "finder"):
                name, _ = self.mac.quit(app)
                if name:
                    closed.append(name)
        dnd, dnd_err = False, ""
        try:
            dnd = self.macapps.set_focus(True)
        except AppError as exc:
            dnd_err = str(exc)
        for a in self.store.alarms(("pending",)):  # one focus timer at a time
            if a.kind == "timer" and a.label == FOCUS_LABEL:
                self.store.set_alarm_status(a.id, "cancelled")
        due = self.clock() + timedelta(minutes=minutes)
        self.store.add_alarm("timer", due, FOCUS_LABEL)
        self.on_change()
        say_en = f"Focus mode is on for {minutes} minutes; I'll ring when the focus timer ends."
        say_hi = f"{minutes} मिनट के लिए फ़ोकस मोड चालू है, टाइमर खत्म होने पर बताऊँगी।"
        if dnd:
            say_en += " Do Not Disturb is on."
            say_hi += " Do Not Disturb चालू है।"
        if closed:
            say_en += f" I closed {_and(closed, False)}."
            say_hi += f" {_and(closed, True)} बंद कर दिए।"
        if not dnd:
            say_en += (f" Do Not Disturb didn't turn on: {dnd_err}." if dnd_err else
                       f" To let me turn on Do Not Disturb too, make a shortcut named “{FOCUS_ON}” (see the user guide).")
        return self._ok("focus.start", say_en, say_hi, hi, minutes=minutes, closed=closed, dnd=dnd,
                        due=due.isoformat(timespec="seconds"))

    def _focus_stop(self, args: dict, hi: bool) -> ToolResult:
        cancelled = 0
        for a in self.store.alarms(("pending",)):
            if a.kind == "timer" and a.label == FOCUS_LABEL:
                self.store.set_alarm_status(a.id, "cancelled")
                cancelled += 1
        if cancelled:
            self.on_change()
        try:
            dnd = self.macapps.set_focus(False)
        except AppError:
            dnd = False
        return self._ok("focus.stop", "Focus mode is off" + (" and Do Not Disturb is off." if dnd else "."),
                        "फ़ोकस मोड बंद" + (", Do Not Disturb भी बंद।" if dnd else "।"), hi, dnd=dnd, cancelled=cancelled)

    # -- music ----------------------------------------------------------------------------------------
    def _music_play(self, args: dict, hi: bool) -> ToolResult:
        q = " ".join(str(args.get("query") or args.get("song") or "").split())[:120]
        if not q:
            return self._media_control({"action": "play"}, hi)
        app = str(args.get("app") or "").lower()
        if "spotify" not in app:
            try:
                playing = self.macapps.play_music(q)
            except AppError as exc:
                playing, err = None, str(exc)
            else:
                err = ""
            if playing:
                return self._ok("music.play", f"Playing {playing}.", f"{playing} चला रही हूँ।", hi, app="Music", playing=playing)
            if "music" in app or self.apps.resolve("Spotify") is None:
                return self._fail("music.play", f"I couldn't find {_q(q)} in your Music library" + (f" ({err})." if err else "."),
                                  f"Music में {_q(q)} नहीं मिला।", hi)
        try:
            self.macapps.open_link(f"spotify:search:{quote(q, safe='')}")
        except AppError:
            res = self._web_open({"target": q, "site": "spotify"}, hi)
            res.tool = "music.play"
            return res
        return self._ok("music.play", f"Searching Spotify for {_q(q)}; pick it from the results.",
                        f"Spotify में {_q(q)} खोज रही हूँ।", hi, app="Spotify")

    # -- clipboard + AI -----------------------------------------------------------------------------------
    def _clipboard_ai(self, args: dict, hi: bool) -> ToolResult:
        text = self._clipboard_text("clipboard.ai", hi)
        if not isinstance(text, str):
            return text
        if self.ai is None:
            return self._fail("clipboard.ai", "I need the AI for that.", "इसके लिए AI चाहिए।", hi)
        task = str(args.get("task") or "summarize").lower()
        task = ("translate" if task.startswith("transl") or task == "anuvad" else "fix" if task in ("fix", "correct", "proofread")
                else "explain" if task.startswith("expl") else "summarize")
        to = " ".join(str(args.get("to") or args.get("language") or "").split())[:30] or ("English" if task == "translate" else "")
        assistant, owner = self.names()
        systems = {
            "summarize": f"Summarise this text for {owner}. {self._spoken_rule(4)} {self._lang_rule(hi)}",
            "explain": f"Explain this text simply for {owner}: what it means and anything to watch out for. "
                       f"{self._spoken_rule(4)} {self._lang_rule(hi)}",
            "translate": f"Translate this text into {to}. Return only the translation, nothing else.",
            "fix": "Correct the spelling, grammar and punctuation of this text, keeping its meaning, tone and language. "
                   "Return only the corrected text.",
        }
        try:
            out = self.ai.write(f"You are {assistant}. {systems[task]}", text[:20000], 900 if task in ("translate", "fix") else 350)
        except (LLMUnavailable, ValueError) as exc:
            return self._fail("clipboard.ai", _end(f"I can't do that right now: {exc}", False), "अभी नहीं हो पाया।", hi)
        out = out.strip()
        if not out:
            return self._fail("clipboard.ai", "I didn't get an answer.", "जवाब नहीं मिला।", hi)
        if task in ("translate", "fix"):
            self.mac.clipboard.set_text(out)
            what = f"Translated into {to}" if task == "translate" else "Fixed it"
            return self._ok("clipboard.ai", f"{what} and copied it to your clipboard: {_q(out, 300)}.",
                            f"{'अनुवाद' if task == 'translate' else 'सुधार'} क्लिपबोर्ड पर कॉपी कर दिया है: {_q(out, 300)}।", hi,
                            task=task, text=out[:5000])
        return self._ok("clipboard.ai", _end(out, hi), _end(out, hi), hi, task=task)

    # -- the daily briefing -------------------------------------------------------------------------------
    def _briefing(self, args: dict, hi: bool) -> ToolResult:
        now = self.clock()
        _, owner = self.names()
        first = owner.split(" ")[0] if owner else ""
        part = "morning" if now.hour < 12 else "afternoon" if now.hour < 17 else "evening"
        en: list[str] = [f"Good {part}{', ' + first if first else ''}. It's {now.strftime('%A')}, {clock_phrase(now, False)}."]
        weekday = ("सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार")[now.weekday()]
        hin: list[str] = [f"नमस्ते{' ' + first if first else ''}। आज {weekday} है, {clock_phrase(now, True)}।"]
        data: dict[str, Any] = {}
        if self.home_city:
            try:
                f = self.weather.forecast(self.home_city)
                en.append(spoken(f, False))
                hin.append(spoken(f, True))
                data["weather"] = {"place": f.place, "now_c": f.now_c, "high_c": f.high_c, "low_c": f.low_c}
            except (WeatherError, OSError) as exc:
                log.info("briefing: no weather (%s)", exc)
        else:
            en.append("Tell me your city once and I'll include the weather.")
        events, _ = self.events_on(now.date())
        upcoming = [e for e in events if e.start >= now - timedelta(minutes=30)]
        data["events"] = [{"title": e.title, "start": e.start.isoformat(timespec="minutes")} for e in upcoming]
        if upcoming:
            items = [f"{e.title} at {clock_phrase(e.start, False)}" for e in upcoming[:4]]
            en.append(f"You have {len(upcoming)} thing{'s' if len(upcoming) != 1 else ''} on your calendar: {_and(items, False)}"
                      + (f", and {len(upcoming) - 4} more." if len(upcoming) > 4 else "."))
            hin.append(f"कैलेंडर में {len(upcoming)} इवेंट: " + _and([f"{e.title} {clock_phrase(e.start, True)}" for e in upcoming[:4]], True) + "।")
        else:
            en.append("Your calendar is clear for the rest of today.")
            hin.append("आज बाकी दिन कैलेंडर खाली है।")
        if self.apple is not None:
            try:
                end = datetime.combine(now.date(), datetime.max.time())
                due = [(t, d) for t, d in self.apple.reminders() if d is not None and d <= end]
                if due:
                    data["reminders"] = [t for t, _ in due]
                    en.append(f"Reminders due: {_and([t for t, _ in due[:4]], False)}.")
                    hin.append(f"रिमाइंडर: {_and([t for t, _ in due[:4]], True)}।")
            except AppleError as exc:
                log.info("briefing: no reminders (%s)", exc)
        if self.google is not None and self.google.auth.has("gmail"):
            try:
                n, more, mails = self.google.gmail.unread(10)
                senders = list(dict.fromkeys(m.sender for m in mails))[:3]
                data["unread"] = n
                if n:
                    en.append(f"You have {n}{'+' if more else ''} unread email{'s' if n != 1 else ''}, from {_and(senders, False)}"
                              + (" and others." if n > len(senders) else "."))
                    hin.append(f"{n} नए ईमेल हैं, {_and(senders, True)} से।")
                else:
                    en.append("No unread email.")
                    hin.append("कोई नया ईमेल नहीं।")
            except GoogleError as exc:
                log.info("briefing: no mail (%s)", exc)
        alarms = [a for a in self.store.alarms(("pending",)) if a.kind == "alarm" and a.due.date() == now.date()]
        if alarms:
            en.append(f"Alarms today: {_and([clock_phrase(a.due, False) for a in alarms[:3]], False)}.")
        return self._ok("briefing", " ".join(en), " ".join(hin), hi, **data)

    # -- screen help ---------------------------------------------------------------------------------------
    def _screen_explain(self, args: dict, hi: bool) -> ToolResult:
        model = getattr(self.ai, "gemini_heavy", None) if self.ai is not None else None
        if model is None:
            return self._fail("screen.explain", "Screen help needs a Gemini key (Settings → AI): the local model can't see images.",
                              "स्क्रीन देखने के लिए Settings → AI में Gemini चाहिए।", hi)
        try:
            image = self.mac.screen_image()
        except PermissionError:
            return self._fail("screen.explain", "macOS needs your permission first: turn on JARVIS in System Settings, Privacy & "
                              "Security, Screen Recording, then ask again.",
                              "स्क्रीन देखने के लिए macOS की अनुमति चाहिए: System Settings, Privacy & Security, Screen Recording।",
                              hi, permission="screen_recording")
        except (OSError, ValueError) as exc:
            return self._fail("screen.explain", _end(f"I couldn't capture the screen: {exc}", False), "स्क्रीन कैप्चर नहीं हो पाई।", hi)
        assistant, owner = self.names()
        question = " ".join(str(args.get("question") or "").split())[:300]
        system = (f"You are {assistant}, {owner}'s voice assistant on their Mac, looking at a screenshot of {owner}'s screen "
                  f"that {owner} asked you to look at. Say, in this order: what is happening on the screen (the app and what "
                  "they are doing); if there is an error or warning, what it says, what it most likely means and how to fix "
                  f"it; then one or two concrete things you can do for {owner} right now. You can: {CAN_DO}. Offer only "
                  "those. If the user asked something specific, answer that first. Never read out passwords, codes, card "
                  f"numbers or other secrets you see. {self._spoken_rule(6)} {self._lang_rule(hi)}")
        try:
            out = self.ai.see(system, question or "What's happening on my screen?", image, "image/jpeg")
        except LLMUnavailable as exc:
            return self._fail("screen.explain", _end(f"I couldn't look at it right now: {exc}", False), "अभी स्क्रीन नहीं देख पाई।", hi)
        finally:
            del image
        if not out:
            return self._fail("screen.explain", "I couldn't make sense of the screen.", "स्क्रीन समझ नहीं आई।", hi)
        return self._ok("screen.explain", _end(out, hi), _end(out, hi), hi, model=model, sent="screenshot to Gemini")
