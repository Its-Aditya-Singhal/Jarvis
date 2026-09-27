"""Executes the actions the language model proposed.

Each tool validates its own arguments (the model's JSON is untrusted) and
returns a ToolResult whose spoken text describes what actually happened, in
English or Hindi. Nothing here runs shell commands with model-supplied text.
Each tool declares the auth level it needs (see ``jarvis.auth.levels``):
1 = read, 2 = create/open/cancel, 3 = delete. Level-3 tools never run
directly: ``plan`` resolves exactly what would be deleted, the owner
confirms, and only then ``execute`` deletes those items.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from rapidfuzz import fuzz

from ..database.db import Database
from ..llm.intents import Action, clock_phrase, day_phrase, parse_local
from .apple import AppleBridge, AppleError
from .apps import AppIndex
from .files import FileSearch
from .mac import SETTINGS_TITLES, SITES, MacControl, settings_page
from .store import Event, ToolStore

log = logging.getLogger(__name__)

LEVELS = {
    "calendar.list": 1, "notes.search": 1, "files.search": 1,
    "alarm.set": 2, "timer.set": 2, "alarm.cancel": 2, "calendar.create": 2, "notes.add": 2, "app.open": 2,
    "calendar.delete": 3, "notes.delete": 3,
    "history.search": 1, "memory.remember": 2, "memory.forget": 3,
    "alarm.list": 1, "system.battery": 1, "system.lock": 1,  # locking only protects
    "app.close": 2, "folder.open": 2, "web.open": 2, "system.volume": 2, "media.control": 2,
    "screen.shot": 2, "display.brightness": 2, "display.dark_mode": 2, "settings.open": 2,
    "clipboard.read": 1, "clipboard.note": 2, "text.type": 2, "ai.ask": 2,
}
# the chat apps "ai.ask" can fill in: app name, website that pre-fills a prompt (None: it would send it), home page
AI_SERVICES = {
    "claude": ("Claude", "https://claude.ai/new?q={}", "https://claude.ai/new"),
    # chatgpt.com/?q= sends the prompt straight away, so the website gets it through the clipboard
    "chatgpt": ("ChatGPT", None, "https://chatgpt.com/"),
}
MATCH_MIN = 75  # fuzzy score needed to pick a note/event to delete
MAX_TIMER_S = 24 * 3600
MAX_AHEAD = timedelta(days=366)


@dataclass
class ToolResult:
    tool: str
    ok: bool
    say: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Plan:
    """A destructive action resolved to one exact item, awaiting confirmation."""

    tool: str
    item_id: int
    what: str  # e.g. the note's text or the event's title and time
    hi: bool


def _quote(s: str, n: int = 60) -> str:
    s = " ".join(s.split())
    return f"“{s if len(s) <= n else s[: n - 1] + '…'}”"


def _join(items: list[str], hi: bool) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + (" और " if hi else " and ") + items[-1]


class ToolRunner:
    def __init__(
        self,
        db: Database,
        store: ToolStore,
        apps: AppIndex,
        files: FileSearch,
        apple: AppleBridge,
        on_change: Callable[[], None] = lambda: None,
        clock: Callable[[], datetime] = datetime.now,
        mac: MacControl | None = None,
    ):
        self.db = db
        self.store = store
        self.apps = apps
        self.files = files
        self.mac = mac or MacControl(extra_folders=lambda: files.folders() if files else [])
        self.apple = apple
        self.on_change = on_change
        self.clock = clock
        self.memory: Any = None  # jarvis.memory.manager.Memory, set when memory is enabled

    # -- Apple sync settings --------------------------------------------------------
    @property
    def calendar_sync(self) -> bool:
        return self.db.get("apple_calendar_sync") == "1"

    @property
    def apple_calendar(self) -> str:
        return self.db.get("apple_calendar_name") or ""

    @property
    def notes_sync(self) -> bool:
        return self.db.get("apple_notes_sync") == "1"

    # -- dispatch ----------------------------------------------------------------------
    def run(self, action: Action, lang: str) -> ToolResult:
        hi = lang != "en"
        handler = getattr(self, "_" + action.tool.replace(".", "_"), None)
        if handler is None:
            return ToolResult(action.tool, False, "यह टूल उपलब्ध नहीं है।" if hi else "That tool isn't available.")
        try:
            return handler(action.args, hi)
        except Exception:
            log.exception("tool %s failed", action.tool)
            return ToolResult(action.tool, False, "यह काम करते समय गड़बड़ हो गई।" if hi else "Something went wrong doing that.")

    def plan(self, action: Action, lang: str) -> Plan | ToolResult:
        """Resolve a level-3 action to the single item it would delete."""
        hi = lang != "en"
        planner = getattr(self, "_plan_" + action.tool.replace(".", "_"), None)
        if planner is None:
            return ToolResult(action.tool, False, "यह टूल उपलब्ध नहीं है।" if hi else "That tool isn't available.")
        return planner(action.args, hi)

    def execute(self, plan: Plan) -> ToolResult:
        hi = plan.hi
        if plan.tool == "notes.delete":
            done = self.store.delete_note(plan.item_id)
        elif plan.tool == "memory.forget" and self.memory is not None:
            done = self.memory.delete(plan.item_id)
        elif plan.tool == "calendar.delete":
            done = self.store.delete_event(plan.item_id)
        else:
            done = False
        if not done:
            return ToolResult(plan.tool, False, "वह पहले ही हट चुका है।" if hi else "That was already gone.")
        self.on_change()
        # copies synced to Apple's apps are left alone: this app never deletes there
        say = f"{plan.what} हटा दिया है।" if hi else f"Deleted {plan.what}."
        return ToolResult(plan.tool, True, say, {"id": plan.item_id})

    # -- alarms & timers ---------------------------------------------------------------
    def _alarm_set(self, args: dict, hi: bool) -> ToolResult:
        now = self.clock()
        t = parse_local(args.get("time"))
        if t is None:
            return ToolResult("alarm.set", False, "अलार्म का समय समझ नहीं आया।" if hi else "I couldn't work out the alarm time.")
        if t <= now:
            return ToolResult("alarm.set", False, "वह समय निकल चुका है।" if hi else "That time has already passed.")
        if t - now > MAX_AHEAD:
            return ToolResult("alarm.set", False, "इतनी दूर का अलार्म नहीं लग सकता।" if hi else "That's too far ahead for an alarm.")
        label = str(args.get("label") or "").strip()[:80]
        aid = self.store.add_alarm("alarm", t, label)
        self.on_change()
        when = f"{day_phrase(t.date(), now.date(), hi)} {clock_phrase(t, hi)}"
        say = (f"{when} का अलार्म लगा दिया है।" if hi
               else f"Alarm set for {clock_phrase(t, False)} {day_phrase(t.date(), now.date(), False)}"
               + (f", {_quote(label)}." if label else "."))
        return ToolResult("alarm.set", True, say, {"id": aid, "time": t.isoformat(timespec="minutes")})

    def _timer_set(self, args: dict, hi: bool) -> ToolResult:
        try:
            secs = int(float(args.get("seconds")))  # type: ignore[arg-type]  # missing -> TypeError, handled
        except (TypeError, ValueError):
            secs = 0
        if not 1 <= secs <= MAX_TIMER_S:
            return ToolResult("timer.set", False, "टाइमर की अवधि समझ नहीं आई।" if hi else "I couldn't work out the timer length.")
        label = str(args.get("label") or "").strip()[:80]
        due = self.clock() + timedelta(seconds=secs)
        tid = self.store.add_alarm("timer", due, label)
        self.on_change()
        m, s = divmod(secs, 60)
        if hi:
            length = f"{m} मिनट" + (f" {s} सेकंड" if s else "") if m else f"{s} सेकंड"
            say = f"{length} का टाइमर शुरू कर दिया है।"
        else:
            length = (f"{m} minute{'s' if m != 1 else ''}" + (f" {s} seconds" if s else "")) if m else f"{s} seconds"
            say = f"Timer started for {length}."
        return ToolResult("timer.set", True, say, {"id": tid, "due": due.isoformat(timespec="seconds")})

    def _alarm_cancel(self, args: dict, hi: bool) -> ToolResult:
        pending = [a for a in self.store.alarms(("pending",))]
        t = parse_local(args.get("time"))
        if t is not None:
            pending = [a for a in pending if a.kind == "alarm" and abs((a.due - t).total_seconds()) < 60]
        if not pending:
            return ToolResult("alarm.cancel", False, "रद्द करने के लिए कोई अलार्म नहीं मिला।" if hi
                              else "There's no matching alarm or timer to cancel.")
        for a in pending:
            self.store.set_alarm_status(a.id, "cancelled")
        self.on_change()
        n = len(pending)
        if hi:
            say = f"{n} अलार्म/टाइमर रद्द कर दिए हैं।" if n > 1 else "अलार्म रद्द कर दिया है।"
        elif n == 1:
            a = pending[0]
            say = (f"Cancelled the {clock_phrase(a.due, False)} alarm." if a.kind == "alarm" else "Cancelled the timer.")
        else:
            say = f"Cancelled {n} alarms and timers."
        return ToolResult("alarm.cancel", True, say, {"ids": [a.id for a in pending]})

    # -- calendar ----------------------------------------------------------------------
    def _calendar_create(self, args: dict, hi: bool) -> ToolResult:
        now = self.clock()
        start = parse_local(args.get("start"))
        title = " ".join(str(args.get("title") or "").split())[:120]
        if start is None or not title:
            return ToolResult("calendar.create", False, "इवेंट का नाम या समय समझ नहीं आया।" if hi else "I couldn't work out the event's title or time.")
        end = parse_local(args.get("end"))
        if end is None or end <= start:
            end = start + timedelta(hours=1)
        eid = self.store.add_event(title, start, end)
        synced, err = False, None
        if self.calendar_sync and self.apple_calendar:
            try:
                self.store.set_event_apple(eid, self.apple.create_event(self.apple_calendar, title, start, end))
                synced = True
            except AppleError as exc:
                err = str(exc)
        self.on_change()
        day = day_phrase(start.date(), now.date(), hi)
        if hi:
            say = f"{day} {clock_phrase(start, True)} {_quote(title)} कैलेंडर में जोड़ दिया है" + (
                ", Apple Calendar में भी।" if synced else "।")
        else:
            say = f"Added {_quote(title)} {day} at {clock_phrase(start, False)}" + (
                ", also in Apple Calendar." if synced else ".")
        if err:
            say += (" Apple Calendar से सिंक नहीं हो पाया।" if hi else f" Apple Calendar sync failed: {err}.")
        return ToolResult("calendar.create", True, say, {"id": eid, "start": start.isoformat(timespec="minutes"), "apple": synced})

    def events_on(self, day: date) -> tuple[list[Event], str | None]:
        d0 = datetime.combine(day, datetime.min.time())
        events = self.store.events_between(d0, d0 + timedelta(days=1))
        err = None
        if self.calendar_sync:
            try:
                known = {e.apple_uid for e in events if e.apple_uid}
                for ae in self.apple.events_on(day):
                    if ae.uid not in known:
                        events.append(Event(None, ae.title, ae.start, ae.start, "apple", ae.uid))
            except AppleError as exc:
                err = str(exc)
        return sorted(events, key=lambda e: e.start), err

    def _calendar_list(self, args: dict, hi: bool) -> ToolResult:
        now = self.clock()
        d = parse_local(f"{args.get('date') or now.date().isoformat()}T00:00")
        day = d.date() if d else now.date()
        events, err = self.events_on(day)
        dp = day_phrase(day, now.date(), hi)
        if not events:
            say = f"{dp} आपका कैलेंडर खाली है।" if hi else f"Your calendar is clear {dp}."
        else:
            items = [f"{e.title} {clock_phrase(e.start, True)}" if hi else f"{e.title} at {clock_phrase(e.start, False)}"
                     for e in events[:5]]
            more = len(events) - len(items)
            if hi:
                say = f"{dp} आपके {len(events)} इवेंट हैं: {_join(items, True)}" + (f", और {more} और।" if more else "।")
            else:
                say = (f"You have {len(events)} event{'s' if len(events) != 1 else ''} {dp}: {_join(items, False)}"
                       + (f", and {more} more." if more else "."))
        if err:
            say += " Apple Calendar नहीं पढ़ पाया।" if hi else f" (Apple Calendar couldn't be read: {err}.)"
        data = {"date": day.isoformat(), "events": [
            {"title": e.title, "start": e.start.isoformat(timespec="minutes"), "source": e.source} for e in events]}
        return ToolResult("calendar.list", True, say, data)

    def _plan_calendar_delete(self, args: dict, hi: bool) -> Plan | ToolResult:
        now = self.clock()
        title = " ".join(str(args.get("title") or "").split())[:120]
        if not title:
            return ToolResult("calendar.delete", False, "कौन सा इवेंट हटाऊँ, समझ नहीं आया।" if hi else "I didn't catch which event to delete.")
        d = parse_local(f"{args.get('date')}T00:00") if args.get("date") else None
        if d is not None:
            a, b = d, d + timedelta(days=1)
        else:
            a, b = now - timedelta(days=1), now + MAX_AHEAD
        scored = [(fuzz.token_set_ratio(title.lower(), e.title.lower()), e) for e in self.store.events_between(a, b)]
        scored = [x for x in scored if x[0] >= MATCH_MIN]
        if not scored:
            return ToolResult("calendar.delete", False, f"{_quote(title)} नाम का कोई इवेंट नहीं मिला।" if hi
                              else f"I couldn't find an event called {_quote(title)}.")
        # best match; ties go to the soonest
        e = sorted(scored, key=lambda x: (-x[0], x[1].start))[0][1]
        day = day_phrase(e.start.date(), now.date(), hi)
        what = (f"{day} {clock_phrase(e.start, True)} का {_quote(e.title)}" if hi
                else f"{_quote(e.title)} {day} at {clock_phrase(e.start, False)}")
        assert e.id is not None  # stored events always have an id
        return Plan("calendar.delete", e.id, what, hi)

    # -- notes -------------------------------------------------------------------------
    def _notes_add(self, args: dict, hi: bool) -> ToolResult:
        text = " ".join(str(args.get("text") or "").split())[:2000]
        if not text:
            return ToolResult("notes.add", False, "नोट में क्या लिखूँ, समझ नहीं आया।" if hi else "I didn't catch what to write in the note.")
        nid = self.store.add_note(text)
        synced, err = False, None
        if self.notes_sync:
            try:
                self.store.set_note_apple(nid, self.apple.create_note(text))
                synced = True
            except AppleError as exc:
                err = str(exc)
        self.on_change()
        say = ("नोट सेव कर दिया है" + (", Apple Notes में भी।" if synced else "।")) if hi else (
            "Note saved" + (", also in Apple Notes." if synced else "."))
        if err:
            say += " Apple Notes से सिंक नहीं हो पाया।" if hi else f" Apple Notes sync failed: {err}."
        return ToolResult("notes.add", True, say, {"id": nid, "apple": synced})

    def _notes_search(self, args: dict, hi: bool) -> ToolResult:
        q = " ".join(str(args.get("query") or "").split())[:100]
        if not q:
            return ToolResult("notes.search", False, "क्या ढूँढूँ, समझ नहीं आया।" if hi else "I didn't catch what to look for.")
        scored = [(fuzz.partial_ratio(q.lower(), n.text.lower()), n) for n in self.store.notes()]
        found = [n.text for s, n in sorted(scored, key=lambda x: -x[0]) if s >= 75][:3]
        apple: list[str] = []
        if self.notes_sync:
            try:
                apple = [t for t in self.apple.search_notes(q) if t not in found][: max(0, 3 - len(found))]
            except AppleError:
                pass
        allnotes = found + apple
        if not allnotes:
            say = f"{_quote(q)} से जुड़ा कोई नोट नहीं मिला।" if hi else f"No notes match {_quote(q)}."
        else:
            listed = _join([_quote(t) for t in allnotes], hi)
            say = f"{len(allnotes)} नोट मिले: {listed}।" if hi else (
                f"I found {len(allnotes)} note{'s' if len(allnotes) != 1 else ''}: {listed}.")
        return ToolResult("notes.search", True, say, {"notes": allnotes})

    def _plan_notes_delete(self, args: dict, hi: bool) -> Plan | ToolResult:
        q = " ".join(str(args.get("query") or "").split())[:100]
        if not q:
            return ToolResult("notes.delete", False, "कौन सा नोट हटाऊँ, समझ नहीं आया।" if hi else "I didn't catch which note to delete.")
        scored = [(fuzz.partial_ratio(q.lower(), n.text.lower()), n) for n in self.store.notes(1000)]
        scored = [x for x in scored if x[0] >= MATCH_MIN]
        if not scored:
            return ToolResult("notes.delete", False, f"{_quote(q)} से जुड़ा कोई नोट नहीं मिला।" if hi else f"No note matches {_quote(q)}.")
        n = sorted(scored, key=lambda x: (-x[0], -x[1].created.timestamp()))[0][1]
        return Plan("notes.delete", n.id, f"नोट {_quote(n.text)}" if hi else f"the note {_quote(n.text)}", hi)

    # -- memory ------------------------------------------------------------------------
    def _memory_remember(self, args: dict, hi: bool) -> ToolResult:
        if self.memory is None:
            return ToolResult("memory.remember", False, "मेमोरी बंद है।" if hi else "Memory is turned off.")
        fact, created = self.memory.remember(str(args.get("text") or ""), "said")
        if fact is None:
            return ToolResult("memory.remember", False, "क्या याद रखूँ, समझ नहीं आया।" if hi else "I didn't catch what to remember.")
        if hi:
            say = "याद रख लिया।" if created else "यह पहले से याद है, अपडेट कर दिया।"
        else:
            say = "Got it, I'll remember that." if created else "I already knew that; updated it."
        return ToolResult("memory.remember", True, say, {"id": fact.id, "text": fact.text})

    def _plan_memory_forget(self, args: dict, hi: bool) -> Plan | ToolResult:
        q = " ".join(str(args.get("query") or "").split())[:200]
        if self.memory is None or not q:
            return ToolResult("memory.forget", False, "क्या भूलूँ, समझ नहीं आया।" if hi else "I didn't catch what to forget.")
        hits = self.memory.search(q, k=1, min_sim=0.5)
        if not hits:
            return ToolResult("memory.forget", False, f"{_quote(q)} के बारे में मुझे कुछ याद नहीं है।" if hi
                              else f"I don't have anything remembered about {_quote(q)}.")
        f = hits[0][1]
        return Plan("memory.forget", f.id, f"याद की गई बात {_quote(f.text)}" if hi else f"the memory {_quote(f.text)}", hi)

    def _history_search(self, args: dict, hi: bool) -> ToolResult:
        if self.memory is None:
            return ToolResult("history.search", False, "मेमोरी बंद है।" if hi else "Memory is turned off.")
        q = " ".join(str(args.get("query") or "").split())[:200]
        d = parse_local(f"{args.get('date')}T00:00") if args.get("date") else None
        found = self.memory.search_history(q, d)
        now = self.clock()
        if not found:
            return ToolResult("history.search", True, "ऐसी कोई पिछली बातचीत नहीं मिली।" if hi
                              else "I couldn't find that in our past conversations.", {"turns": []})
        parts = []
        for you, reply in found[:2]:
            when = f"{day_phrase(you.ts.date(), now.date(), hi)} {clock_phrase(you.ts, hi)}"
            if hi:
                parts.append(f"{when} आपने कहा {_quote(you.text)}" + (f", मैंने जवाब दिया {_quote(reply.text)}" if reply else ""))
            else:
                parts.append(f"{when} you said {_quote(you.text)}" + (f" and I replied {_quote(reply.text)}" if reply else ""))
        say = ("; ".join(parts) + ("।" if hi else "."))
        say = say[:1].upper() + say[1:]
        data = {"turns": [{"time": y.ts.isoformat(timespec="minutes"), "you": y.text, "reply": r.text if r else None}
                          for y, r in found]}
        return ToolResult("history.search", True, say, data)

    # -- apps & files --------------------------------------------------------------------
    def _app_open(self, args: dict, hi: bool) -> ToolResult:
        name = str(args.get("name") or "").strip()[:60]
        opened = self.apps.open(name) if name else None
        if opened is None:
            # "open documents" / "open youtube": a folder or website, not an app
            if name and self.mac.folder(name) is not None:
                return self._folder_open({"name": name}, hi)
            if name.lower() in SITES:
                return self._web_open({"target": name}, hi)
            return ToolResult("app.open", False,
                              f"{_quote(name)} नाम का ऐप नहीं मिला।" if hi else f"I couldn't find an app called {_quote(name)}.")
        return ToolResult("app.open", True, f"{opened} खोल दिया है।" if hi else f"Opening {opened}.", {"app": opened})

    def _app_close(self, args: dict, hi: bool) -> ToolResult:
        name = str(args.get("name") or "").strip()[:60]
        closed, running = self.mac.quit(name)
        if closed is None:
            return ToolResult("app.close", False, f"{_quote(name)} अभी खुला नहीं है।" if hi
                              else f"{_quote(name)} isn't open.", {"running": running})
        return ToolResult("app.close", True, f"{closed} बंद किया जा रहा है।" if hi else f"Closing {closed}.", {"app": closed})

    def _folder_open(self, args: dict, hi: bool) -> ToolResult:
        name = str(args.get("name") or "").strip()[:60]
        path = self.mac.folder(name)
        if path is None:
            return ToolResult("folder.open", False, f"{_quote(name)} फ़ोल्डर नहीं मिला।" if hi
                              else f"I don't know a folder called {_quote(name)}.")
        self.mac.open_folder(path)
        label = path.name or "Home"
        return ToolResult("folder.open", True, f"{label} फ़ोल्डर खोल दिया है।" if hi else f"Opening your {label} folder.",
                          {"path": str(path)})

    def _web_open(self, args: dict, hi: bool) -> ToolResult:
        target = " ".join(str(args.get("target") or args.get("url") or args.get("query") or "").split())[:200]
        if not target:
            return ToolResult("web.open", False, "क्या खोलूँ, समझ नहीं आया।" if hi else "I didn't catch what to open.")
        url, what = self.mac.site_url(target)
        self.mac.open_url(url)
        return ToolResult("web.open", True, f"ब्राउज़र में {what} खोल दिया है।" if hi else f"Opening {what} in your browser.",
                          {"url": url})

    def _system_volume(self, args: dict, hi: bool) -> ToolResult:
        cur, muted = self.mac.volume()
        if "mute" in args:
            on = bool(args["mute"])
            self.mac.mute(on)
            say = ("आवाज़ बंद कर दी है।" if on else "आवाज़ चालू कर दी है।") if hi else ("Muted." if on else "Sound is back on.")
            return ToolResult("system.volume", True, say, {"muted": on})
        if "level" in args and str(args["level"]).lstrip("-").isdigit():
            level = int(args["level"])
        elif "change" in args and str(args["change"]).lstrip("-").isdigit():
            level = cur + int(args["change"])
        else:
            return ToolResult("system.volume", True, f"आवाज़ {cur}% पर है।" if hi else f"Volume is at {cur}%"
                              + (", muted." if muted else "."), {"level": cur, "muted": muted})
        level = self.mac.set_volume(level)
        return ToolResult("system.volume", True, f"आवाज़ {level}% कर दी है।" if hi else f"Volume set to {level}%.",
                          {"level": level})

    def _media_control(self, args: dict, hi: bool) -> ToolResult:
        action = str(args.get("action") or "toggle").lower()
        if action not in ("play", "pause", "toggle", "next", "previous"):
            action = "toggle"
        player = self.mac.media(action)
        if player is None:
            return ToolResult("media.control", False, "कोई म्यूज़िक नहीं चल रहा।" if hi else "Nothing is playing.")
        en = {"play": "Playing", "pause": "Paused", "toggle": "Done", "next": "Next track", "previous": "Previous track"}
        hin = {"play": "चला दिया", "pause": "रोक दिया", "toggle": "कर दिया", "next": "अगला गाना", "previous": "पिछला गाना"}
        return ToolResult("media.control", True, f"{player}: {hin[action]}।" if hi else f"{en[action]} on {player}.",
                          {"player": player})

    def _system_battery(self, args: dict, hi: bool) -> ToolResult:
        pct, plugged = self.mac.battery()
        if pct is None:
            return ToolResult("system.battery", True, "यह Mac बिजली से चलता है।" if hi else "This Mac runs on mains power.")
        state = ("चार्ज हो रही है" if hi else "plugged in") if plugged else ("बैटरी पर" if hi else "on battery")
        return ToolResult("system.battery", True, f"बैटरी {pct}% है, {state}।" if hi else f"Battery is at {pct}%, {state}.",
                          {"percent": pct, "plugged": plugged})

    def _system_lock(self, args: dict, hi: bool) -> ToolResult:
        self.mac.lock()
        return ToolResult("system.lock", True, "स्क्रीन लॉक कर दी है।" if hi else "Locking the screen.")

    # -- screen & display ---------------------------------------------------------------
    def _screen_shot(self, args: dict, hi: bool) -> ToolResult:
        to_clip = str(args.get("to") or "").lower() == "clipboard"
        try:
            path = self.mac.screenshot(to_clipboard=to_clip)
        except PermissionError:
            return ToolResult("screen.shot", False, "स्क्रीनशॉट के लिए macOS की अनुमति चाहिए: System Settings, Privacy & Security, "
                              "Screen Recording में JARVIS को चालू कीजिए, फिर दोबारा कहिए।" if hi else
                              "macOS needs your permission first: turn on JARVIS in System Settings, Privacy & Security, "
                              "Screen Recording, then ask again.", {"permission": "screen_recording"})
        if path is None:
            return ToolResult("screen.shot", True, "स्क्रीनशॉट क्लिपबोर्ड में कॉपी कर दिया है।" if hi
                              else "Screenshot copied to the clipboard.", {"clipboard": True})
        where = path.parent.name or "Home"
        return ToolResult("screen.shot", True, f"स्क्रीनशॉट {where} में सेव कर दिया है।" if hi
                          else f"Screenshot saved to your {where}.", {"path": str(path)})

    def _display_brightness(self, args: dict, hi: bool) -> ToolResult:
        cur = self.mac.get_brightness()
        if cur is None:
            return ToolResult("display.brightness", False, "इस स्क्रीन की ब्राइटनेस मैं नहीं बदल सकता।" if hi
                              else "I can't control this screen's brightness.")
        if "level" in args and str(args["level"]).lstrip("-").isdigit():
            level = int(args["level"])
        elif "change" in args and str(args["change"]).lstrip("-").isdigit():
            level = cur + int(args["change"])
        else:
            return ToolResult("display.brightness", True, f"ब्राइटनेस {cur}% पर है।" if hi else f"Brightness is at {cur}%.",
                              {"level": cur})
        done = self.mac.set_brightness(level)
        if done is None:
            return ToolResult("display.brightness", False, "ब्राइटनेस नहीं बदल पाया।" if hi else "I couldn't change the brightness.")
        return ToolResult("display.brightness", True, f"ब्राइटनेस {done}% कर दी है।" if hi else f"Brightness set to {done}%.",
                          {"level": done})

    def _display_dark_mode(self, args: dict, hi: bool) -> ToolResult:
        cur = self.mac.dark_mode()
        want = args.get("on")
        if isinstance(want, str):
            want = {"true": True, "on": True, "false": False, "off": False}.get(want.lower())
        on = (not cur) if want is None else bool(want)
        if on != cur:
            self.mac.set_dark_mode(on)
        if hi:
            say = "डार्क मोड चालू कर दिया है।" if on else "लाइट मोड चालू कर दिया है।"
        else:
            say = ("Dark mode is on." if on else "Light mode is on.") if on != cur else (
                "Dark mode is already on." if on else "Light mode is already on.")
        return ToolResult("display.dark_mode", True, say, {"dark": on})

    def _settings_open(self, args: dict, hi: bool) -> ToolResult:
        spoken = " ".join(str(args.get("page") or "").split())[:60]
        root = re.fullmatch(r"(?:(?:mac |system )?(?:settings|preferences)|system settings|)", spoken.lower())
        pane = None if root else settings_page(spoken)
        if pane is None and not root:
            self.mac.open_settings(None)
            return ToolResult("settings.open", True, f"{_quote(spoken)} वाला पेज नहीं पता, System Settings खोल दी है।" if hi
                              else f"I don't know a {_quote(spoken)} page, so I opened System Settings.", {"page": None})
        self.mac.open_settings(pane)
        title = SETTINGS_TITLES[pane] if pane else "System Settings"
        return ToolResult("settings.open", True, f"{title} खोल दिया है।" if hi else f"Opening {title}"
                          + (" in System Settings." if pane else "."), {"page": pane})

    # -- clipboard & typing ---------------------------------------------------------------
    def _clipboard_text(self, tool: str, hi: bool) -> str | ToolResult:
        clip = self.mac.clipboard
        if clip.concealed():
            return ToolResult(tool, False, "क्लिपबोर्ड में पासवर्ड है, इसलिए मैं उसे नहीं पढ़ूँगा।" if hi
                              else "Your clipboard holds a password, so I'll leave it alone.", {"concealed": True})
        text = (clip.text() or "").strip()
        if not text:
            return ToolResult(tool, False, "क्लिपबोर्ड में कोई टेक्स्ट नहीं है।" if hi
                              else "There's no text on your clipboard.")
        return text

    def _clipboard_read(self, args: dict, hi: bool) -> ToolResult:
        text = self._clipboard_text("clipboard.read", hi)
        if isinstance(text, ToolResult):
            return text
        spoken = _quote(text, 200)
        return ToolResult("clipboard.read", True, f"क्लिपबोर्ड में है: {spoken}।" if hi else f"Your clipboard says {spoken}.",
                          {"text": text[:5000]})

    def _clipboard_note(self, args: dict, hi: bool) -> ToolResult:
        text = self._clipboard_text("clipboard.note", hi)
        if isinstance(text, ToolResult):
            return text
        res = self._notes_add({"text": text}, hi)
        res.tool = "clipboard.note"
        if res.ok:  # "Note saved, also in Apple Notes." -> "Saved your clipboard as a note, also in Apple Notes."
            res.say = res.say.replace("नोट सेव कर दिया है", "क्लिपबोर्ड वाला टेक्स्ट नोट में सेव कर दिया है", 1) if hi else \
                res.say.replace("Note saved", "Saved your clipboard as a note", 1)
        return res

    def _text_type(self, args: dict, hi: bool) -> ToolResult:
        text = str(args.get("text") or "").strip()[:5000]
        if not text:
            return ToolResult("text.type", False, "क्या टाइप करूँ, समझ नहीं आया।" if hi else "I didn't catch what to type.")
        try:
            app = self.mac.type_text(text)
        except LookupError:
            return ToolResult("text.type", False, "पहले उस ऐप पर जाइए जहाँ टाइप करना है, फिर कहिए।" if hi
                              else "Switch to the app you want me to type in, then ask again.")
        except PermissionError:
            return ToolResult("text.type", False, "टाइप करने के लिए macOS की अनुमति चाहिए: System Settings, Privacy & Security, "
                              "Accessibility में JARVIS को चालू कीजिए।" if hi else
                              "macOS needs your permission first: turn on JARVIS in System Settings, Privacy & Security, "
                              "Accessibility, then ask again.", {"permission": "accessibility"})
        return ToolResult("text.type", True, f"{app} में टाइप कर दिया है।" if hi else f"Typed it into {app}.",
                          {"app": app, "chars": len(text)})

    # -- Claude / ChatGPT --------------------------------------------------------------------
    def _ai_ask(self, args: dict, hi: bool) -> ToolResult:
        spoken = re.sub(r"[^a-z]", "", str(args.get("service") or "claude").lower())
        service = "chatgpt" if spoken in ("chatgpt", "gpt", "openai", "chatgbt") else "claude"
        prompt = " ".join(str(args.get("prompt") or "").split())[:4000]
        name, web, home = AI_SERVICES[service]
        if not prompt:
            return ToolResult("ai.ask", False, f"{name} से क्या पूछूँ, समझ नहीं आया।" if hi else f"I didn't catch what to ask {name}.")
        found = self.apps.resolve(name) if self.apps is not None else None
        app = found[1] if found is not None and found[0] == name else None
        how = self.mac.ask_ai(name, app, prompt, web, home)
        q = _quote(prompt, 120)
        if how == "clipboard":
            say = (f"{name} खोल दिया है। आपका सवाल {q} क्लिपबोर्ड पर है: Command-V से पेस्ट करके Return दबाइए।" if hi else
                   f"Opening {name}. Your prompt {q} is on the clipboard: paste it with Command-V, then press Return to send.")
        else:
            where = "" if how == "app" else (" ब्राउज़र में" if hi else " in your browser")
            say = (f"{name}{where} खोल दिया है, सवाल लिखा हुआ है: {q}। भेजने के लिए Return दबाइए।" if hi else
                   f"{name} is open{where} with your prompt {q}. Press Return to send it.")
        return ToolResult("ai.ask", True, say, {"service": service, "via": how, "prompt": prompt})

    def _alarm_list(self, args: dict, hi: bool) -> ToolResult:
        now = self.clock()
        items = sorted(self.store.alarms(("pending", "ringing")), key=lambda a: a.due)
        if not items:
            return ToolResult("alarm.list", True, "कोई अलार्म या टाइमर नहीं लगा है।" if hi else "You have no alarms or timers set.")
        parts = []
        for a in items[:4]:
            if a.kind == "timer":
                left = max(0, int((a.due - now).total_seconds()))
                parts.append(f"{left // 60} मिनट बाकी वाला टाइमर" if hi else
                             f"a timer with {left // 60} min {left % 60} s left" if left >= 60 else f"a timer with {left} s left")
            else:
                parts.append(f"{day_phrase(a.due.date(), now.date(), True)} {clock_phrase(a.due, True)} का अलार्म" if hi else
                             f"an alarm at {clock_phrase(a.due, False)} {day_phrase(a.due.date(), now.date(), False)}")
        what = _join(parts, hi)
        return ToolResult("alarm.list", True, f"आपके पास {what} है।" if hi else f"You have {what}.", {"count": len(items)})

    def _files_search(self, args: dict, hi: bool) -> ToolResult:
        q = " ".join(str(args.get("query") or "").split())[:100]
        if not q:
            return ToolResult("files.search", False, "क्या ढूँढूँ, समझ नहीं आया।" if hi else "I didn't catch what to look for.")
        hits = self.files.search(q)
        blocked = [f.name for f in self.files.denied()]
        if not hits:
            say = f"अनुमति वाले फ़ोल्डरों में {_quote(q)} से जुड़ी कोई फ़ाइल नहीं मिली।" if hi else (
                f"No files matching {_quote(q)} in your allowed folders.")
        else:
            names = _join([h.name for h in hits[:3]], hi)
            say = f"{len(hits)} फ़ाइलें मिलीं, जैसे {names}।" if hi else (
                f"I found {len(hits)} file{'s' if len(hits) != 1 else ''}"
                + (f", including {names}." if len(hits) > 3 else f": {names}."))
        if blocked:
            which = _join(blocked, hi)
            say += (f" macOS ने मुझे {which} देखने की अनुमति नहीं दी है — System Settings, Privacy & Security, "
                    "Files and Folders में अनुमति दीजिए।" if hi else
                    f" macOS hasn't let me look in {which} — allow it in System Settings, Privacy & Security, "
                    "Files and Folders.")
        return ToolResult("files.search", True, say, {"files": [str(h) for h in hits], "denied": blocked})

    # -- manual sync ----------------------------------------------------------------------
    def sync_now(self) -> dict:
        """Push local items that aren't in Apple's apps yet (upcoming events, all notes)."""
        pushed = {"events": 0, "notes": 0}
        if self.calendar_sync and self.apple_calendar:
            for e in self.store.unsynced_events():
                assert e.id is not None
                self.store.set_event_apple(e.id, self.apple.create_event(self.apple_calendar, e.title, e.start, e.end))
                pushed["events"] += 1
        if self.notes_sync:
            for n in self.store.unsynced_notes():
                self.store.set_note_apple(n.id, self.apple.create_note(n.text))
                pushed["notes"] += 1
        self.on_change()
        return pushed
