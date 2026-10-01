"""Intent extraction: command text -> structured JSON via the local LLM.

The model fills a fixed JSON schema (Ollama structured output):
  actions   zero or more tool requests from a fixed catalogue, with arguments
  reply     the answer for questions/chat (spoken aloud, so short)

Everything that must be reliable is decided in code, not by the model:
  * the reply language (``detect_language``: speech-recognition result,
    script, and a Hinglish word list), passed to the model per message;
  * the spoken description of each action (``describe``), built from the
    arguments, and the reply for action requests (``compose_reply``), so the
    model can never claim that something was done. Tools only arrive in the
    tools phase; until then nothing is executed.

The system prompt and few-shot examples are constant so Ollama can reuse its
cached prefix; the current time travels with each user message.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..speech.text import has_devanagari

TOOLS: dict[str, tuple[str, str]] = {
    "alarm.set": ("Set an alarm", "time: local ISO datetime like 2026-09-28T07:00; label: optional"),
    "timer.set": ("Start a countdown timer", "seconds: integer; label: optional"),
    "stopwatch": ("Start, stop, reset or read the stopwatch", "action: start | stop | reset | status"),
    "alarm.cancel": ("Cancel alarms/timers", "kind: optional alarm | timer (only that kind); time: optional local ISO datetime of the alarm; omit both to cancel all"),
    "calendar.create": ("Add a calendar event", "title; start: local ISO datetime; end: optional"),
    "calendar.list": ("Read the calendar for a day", "date: ISO date"),
    "calendar.delete": ("Delete a calendar event", "title; date: optional ISO date"),
    "notes.add": ("Save a note", "text"),
    "notes.search": ("Find saved notes", "query"),
    "notes.delete": ("Delete one saved note", "query: words from the note"),
    "app.open": ("Open a Mac application", "name"),
    "app.close": ("Quit (close) a running Mac application", "name"),
    "folder.open": ("Open a folder in Finder (Documents, Downloads, Desktop, Pictures, Music, Movies, Home…)", "name"),
    "web.open": ("Open a website or search the web in the browser", "target: site name, URL, or search words; site: optional youtube | spotify (search there)"),
    "system.volume": ("Change or read the volume", "level: 0-100 | change: +/-number | mute: true/false; no args = read"),
    "media.control": ("Control music playback", "action: play | pause | next | previous"),
    "system.battery": ("Read the battery level", "none"),
    "system.lock": ("Lock the screen", "none"),
    "alarm.list": ("List alarms and timers that are set", "none"),
    "screen.shot": ("Take a screenshot of the whole screen", "to: clipboard (optional; default saves a file)"),
    "display.brightness": ("Change or read the screen brightness", "level: 0-100 | change: +/-number; no args = read"),
    "display.dark_mode": ("Turn dark mode on or off", "on: true/false; omit to toggle"),
    "settings.open": ("Open a System Settings page (Wi-Fi, Bluetooth, Sound, Displays, Battery, Privacy…)", "page"),
    "clipboard.read": ("Read out the text on the clipboard", "none"),
    "clipboard.note": ("Save the clipboard's text as a note", "none"),
    "text.type": ("Type (paste) dictated text into the app in front", "text: exactly what to type"),
    "ai.ask": ("Open Claude or ChatGPT with a prompt written in (the user sends it)", "service: claude | chatgpt; prompt: the request, in the user's words"),
    "memory.remember": ("Remember something the user tells you to remember", "text: the fact, in the user's words"),
    "memory.forget": ("Forget one remembered fact", "query: words from the fact"),
    "history.search": ("Find what was said in past conversations", "query; date: optional ISO date"),
    "files.search": ("Search the user's files by name or content", "query"),
    "files.recent": ("List recent files, newest first (\"the PDF I downloaded yesterday\": kind pdf, when yesterday, folder downloads)",
                     "kind: pdf|image|screenshot|document|spreadsheet|presentation|video|audio|archive|any; when: optional today|yesterday|this week|last week|this month|last N days|ISO date; folder: optional downloads|desktop|documents; query: optional words from the file name"),
    "files.reveal": ("Show a file in Finder", "same args as files.recent (the newest match); no args = the file just found"),
    "files.trash": ("Move one file to the Trash", "same args as files.recent (the newest match); no args = the file just found"),
    "email.unread": ("How many unread emails and from whom (Gmail)", "none"),
    "email.summary": ("Summarise recent emails in the inbox (Gmail)", "count: integer, default 10; from: optional sender name or address; query: optional Gmail search words"),
    "email.read": ("Read / tell what one email says: the newest one matching (\"what did Rahul mail me\")", "from: optional sender name, or him/her for the person just discussed; query: optional search words"),
    "email.draft": ("Write an email and save it as a Gmail draft (not sent)", "to: name, email address, or him/her for the person just discussed; about: what it should say, in the user's words"),
    "email.send": ("Send an email (it is read back and the user must confirm first). With no about: sends the latest draft", "to: name, address or him/her; about: what it should say (omit to send the draft just written)"),
    "drive.search": ("Find files in Google Drive", "query: words from the file name or contents"),
    "drive.recent": ("List the latest files in Google Drive", "none"),
    "drive.summarize": ("Read and summarise a Google Drive document", "query: the file's name (omit for the file just found)"),
    "drive.open": ("Open a Google Drive file in the browser", "query: the file's name (omit for the file just found)"),
    "wait": ("Wait before doing the actions that follow it (\"close it after 10 seconds\": wait, then app.close)", "seconds: integer"),
    "mac.do": ("Anything else a Mac app can do that no tool above covers (Reminders, Music playlists, Messages, Mail, Safari tabs, "
               "Finder windows, Wi-Fi, Do Not Disturb…). JARVIS writes an AppleScript for it and asks the user before changing anything",
               "task: the request in plain English, complete and self-contained"),
}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"tool": {"type": "string", "enum": list(TOOLS)}, "args": {"type": "object"}},
                "required": ["tool", "args"],
            },
        },
        "reply": {"type": "string"},
    },
    "required": ["actions", "reply"],
}

# common Hindi words in Latin script -> the utterance is Hinglish, not English
_HINGLISH = set("""
mujhe mujhko mera meri mere mein main hum aap tum tu kya kaise kaisa kaisi kab kahan kyun kaun
hai hain tha thi the ho hoon hun karo kar karna karke kardo krdo kr bana banao banado do dena dedo
lo lena lene le lagao laga lagado rakho rakh bolo bol batao bata sunao suna khol kholo band chalu
kal aaj abhi parso subah shaam sham raat dopahar baje bajay ghante minute wala wali wale
aur ya bhi nahi nahin na haan ji ka ki ke ko se par pe tak liye zara jaldi thoda bahut accha acha
theek thik yaad dilana dilao jagana jaga uthana utha doodh paani khana ghar
""".split())

LANG_NAME = {"en": "English", "hi": "Hindi", "hinglish": "Hindi"}


def detect_language(text: str, stt_lang: str = "en") -> str:
    """en | hi (Devanagari) | hinglish (Hindi in Latin letters)."""
    if has_devanagari(text):
        return "hi"
    tokens = re.findall(r"[a-z]+", text.lower())
    hits = sum(t in _HINGLISH for t in tokens)
    if tokens and (hits >= 2 and hits / len(tokens) >= 0.25):
        return "hinglish"
    return "hinglish" if stt_lang == "hi" else "en"


def system_prompt(assistant: str, owner: str) -> str:
    tools = "\n".join(f"- {k}: {d} ({a})" for k, (d, a) in TOOLS.items())
    return f"""You are {assistant}, a private voice assistant on {owner}'s Mac that acts through the tools below.
Each user message starts with a header: [Now: <local date and time> | Language: <reply language>].

Return JSON with:
- "actions": one entry for EACH thing the user asks you to do with these tools, in the order asked (a request can contain several); empty for questions and chat:
{tools}
  Resolve relative times ("tomorrow at 7", "kal subah saat baje", "in 10 minutes") to absolute local ISO values from the header's Now. Never add a timezone offset.
- "reply": for questions and chat, a short answer (at most two sentences, spoken aloud: no lists, markdown or emoji), written in the header's Language (Hindi means Devanagari script). Leave it empty when there are actions.

Rules:
- Only include actions requested in the LATEST message. Earlier messages are context only (for follow-ups like "another one at 7:30"); never repeat their actions.
- Split requests joined by "and", "then", "aur", "phir" into separate actions, one per tool use.
- Never say that you did, set, saved or opened anything.
- Prefer the specific tools above. For any other request to DO something on the Mac, use mac.do with the task in plain English ("send an iMessage to Mom saying I'm on my way", "add milk to my Reminders"). Never use mac.do for questions you can answer yourself, for purchases, passwords, deleting files or folders, or running terminal code: for those return no actions and say briefly that you can't do that. files.trash moves exactly one file to the Trash after the user confirms.
- A delay ("after 10 seconds", "in 5 minutes", "10 second baad") is a wait action placed before the actions it delays. "it" means the thing named just before.
- Only use system.volume when the user talks about volume or sound, and display.brightness only for brightness. A number that belongs to a delay or a timer is never a volume or brightness level.
- For live information (weather, news, prices, scores) say you can't look that up live (but web.open can open a website or search for the user).
- The header's Now is the real current local date and time. Answer time, date and day questions from it directly; never tell the user to check a clock.
- A line "[Remembered: ...]" before the user's words lists facts the user earlier asked you to remember, in their own words ("my", "I" = the user). Use them to answer questions; never call memory.remember for them again.
- Only use memory.remember when the user explicitly asks you to remember something ("remember", "yaad rakhna", "don't forget").
- Questions about the user's own life (family, preferences, plans, where things are): answer ONLY from the [Remembered: ...] line. If the answer isn't there, say you don't know yet and that they can ask you to remember it. Never guess personal details. Never say you remember something unless it is in [Remembered: ...].
- Email and Google Drive: "mail", "email", "inbox" mean Gmail; "drive", "google docs" mean Google Drive (files.search is only for files on this Mac). "him", "her", "them" in an email request mean the person from the conversation: use their name from earlier results, or pass the pronoun. "Send it" right after a draft is email.send with no about.
- A line "result" in your earlier answers says what actually happened; use it to resolve follow-ups.
- The user is {owner}."""


def _header(now: datetime, lang: str) -> str:
    return f"[Now: {now.strftime('%A %Y-%m-%d %H:%M')} | Language: {LANG_NAME[lang]}]"


_EX_NOW = datetime(2026, 1, 10, 20, 0)  # examples carry their own "now"
FEWSHOT: list[tuple[str, str, dict]] = [
    ("en", "Wake me up at 6:30 tomorrow and add a note to call the bank",
     {"actions": [{"tool": "alarm.set", "args": {"time": "2026-01-11T06:30"}},
                  {"tool": "notes.add", "args": {"text": "Call the bank"}}], "reply": ""}),
    ("hinglish", "kal shaam 5 baje team meeting calendar mein daal do aur 10 minute ka timer laga do",
     {"actions": [{"tool": "calendar.create", "args": {"title": "Team meeting", "start": "2026-01-11T17:00"}},
                  {"tool": "timer.set", "args": {"seconds": 600}}], "reply": ""}),
    ("en", "Open Notes and find my files about the lease",
     {"actions": [{"tool": "app.open", "args": {"name": "Notes"}},
                  {"tool": "files.search", "args": {"query": "lease"}}], "reply": ""}),
    ("hinglish", "bank wala note delete kar do",
     {"actions": [{"tool": "notes.delete", "args": {"query": "bank"}}], "reply": ""}),
    ("en", "Remember that my passport number ends in 42 and find my passport scan",
     {"actions": [{"tool": "memory.remember", "args": {"text": "My passport number ends in 42"}},
                  {"tool": "files.search", "args": {"query": "passport"}}], "reply": ""}),
    ("en", "Find the PDF I downloaded yesterday and show it in Finder",
     {"actions": [{"tool": "files.recent", "args": {"kind": "pdf", "when": "yesterday", "folder": "downloads"}},
                  {"tool": "files.reveal", "args": {}}], "reply": ""}),
    ("en", "Close Safari and open my downloads folder",
     {"actions": [{"tool": "app.close", "args": {"name": "Safari"}},
                  {"tool": "folder.open", "args": {"name": "Downloads"}}], "reply": ""}),
    ("hinglish", "gaana roko aur volume thoda kam karo",
     {"actions": [{"tool": "media.control", "args": {"action": "pause"}},
                  {"tool": "system.volume", "args": {"change": -10}}], "reply": ""}),
    ("en", "Could you tell me what time it is?", {"actions": [], "reply": "It's 8:00 PM."}),
    ("en", "Who wrote Hamlet?", {"actions": [], "reply": "Hamlet was written by William Shakespeare."}),
    ("hi", "आज मौसम कैसा है?", {"actions": [], "reply": "अभी के मौसम की जानकारी मेरे पास नहीं है, कहें तो वेब पर खोज दूँ।"}),
    ("en", "Open WhatsApp and close it after 10 seconds",
     {"actions": [{"tool": "app.open", "args": {"name": "WhatsApp"}}, {"tool": "wait", "args": {"seconds": 10}},
                  {"tool": "app.close", "args": {"name": "WhatsApp"}}], "reply": ""}),
    ("en", "Add buy milk to my reminders and play my workout playlist",
     {"actions": [{"tool": "mac.do", "args": {"task": "Add a reminder 'Buy milk' in the Reminders app"}},
                  {"tool": "mac.do", "args": {"task": "Play the playlist named 'Workout' in the Music app"}}], "reply": ""}),
    ("en", "Buy me a new phone on Amazon", {"actions": [], "reply": "Sorry, I can't make purchases."}),
    ("en", "Did Rahul send me anything about the trip?",
     {"actions": [{"tool": "email.read", "args": {"from": "Rahul", "query": "trip"}}], "reply": ""}),
    ("en", "Okay, mail him back saying I'll be there by 7",
     {"actions": [{"tool": "email.send", "args": {"to": "him", "about": "Confirm I'll be there by 7"}}], "reply": ""}),
    ("hinglish", "mere last 5 mails ka summary do",
     {"actions": [{"tool": "email.summary", "args": {"count": 5}}], "reply": ""}),
    ("en", "Write a draft to priya@example.com asking for the slides from Monday",
     {"actions": [{"tool": "email.draft", "args": {"to": "priya@example.com", "about": "Ask for the slides from Monday's meeting"}}], "reply": ""}),
    ("en", "What does the project plan doc in my drive say?",
     {"actions": [{"tool": "drive.summarize", "args": {"query": "project plan"}}], "reply": ""}),
]


def build_messages(
    assistant: str, owner: str, now: datetime, lang: str, text: str, history: list[tuple[str, str, str]],
    remembered: list[str] | None = None,
) -> list[dict[str, str]]:
    """history: (language, user text, assistant JSON) of recent exchanges."""
    msgs = [{"role": "system", "content": system_prompt(assistant, owner)}]
    for ex_lang, user, example in FEWSHOT:
        msgs.append({"role": "user", "content": f"{_header(_EX_NOW, ex_lang)}\n{user}"})
        msgs.append({"role": "assistant", "content": json.dumps(example, ensure_ascii=False)})
    for h_lang, user, out in history:
        msgs.append({"role": "user", "content": f"{_header(now, h_lang)}\n{user}"})
        msgs.append({"role": "assistant", "content": out})
    memo = f"[Remembered: {' | '.join(remembered)}]\n" if remembered else ""
    msgs.append({"role": "user", "content": f"{_header(now, lang)}\n{memo}{text}"})
    return msgs


@dataclass
class Action:
    tool: str
    args: dict[str, Any]
    summary: str = ""


@dataclass
class Intent:
    language: str
    actions: list[Action] = field(default_factory=list)
    reply: str = ""


# a small model sometimes adds actions nobody asked for (a delay's "10" became "volume 10%"):
# these tools need a word about them in the request
_GROUNDS = {
    "system.volume": r"volume|sound|loud|quiet|soft|mute|audio|awaa?z|aawaz|dheer|tez|speaker",
    "display.brightness": r"bright|dim|dark|roshni|screen|display",
    "system.lock": r"lock",
    "screen.shot": r"screen ?shot|screen ?grab|capture|screen",
}


def grounded(tool: str, text: str) -> bool:
    """Does the request mention what this tool acts on? (Always true for other tools and Devanagari.)"""
    pat = _GROUNDS.get(tool)
    return pat is None or not text or has_devanagari(text) or re.search(pat, text.lower()) is not None


def parse_intent(data: dict[str, Any], language: str, now: datetime, text: str = "") -> Intent:
    """Validate the model's JSON; unknown tools are dropped, not trusted, and so are tools
    the request never mentioned (``grounded``)."""
    actions: list[Action] = []
    for a in data.get("actions") or []:
        if not isinstance(a, dict) or a.get("tool") not in TOOLS or not grounded(a["tool"], text):
            continue
        args: dict[str, Any] = dict(a["args"]) if isinstance(a.get("args"), dict) else {}
        secs = whole_seconds(args.get("seconds"))
        if a["tool"] in ("wait", "timer.set") and secs is not None:
            args["seconds"] = secs  # 10.0 or "10" -> 10
        if a["tool"] == "wait" and (actions and actions[-1].tool == "wait" or secs is None):
            continue
        if a["tool"] == "mac.do" and actions and actions[-1].tool == "mac.do":
            # one script (and one confirmation) for "add milk to reminders and play my workout playlist":
            # a second script would be refused while the first waits for its confirmation
            prev = actions[-1]
            first, then = str(prev.args.get("task") or "").strip().rstrip("."), str(args.get("task") or "").strip()
            if first and then:
                prev.args = {**prev.args, "task": f"{first}. Then: {then}"}
                prev.summary = describe(prev, language, now)
                continue
        act = Action(a["tool"], args)
        act.summary = describe(act, language, now)
        actions.append(act)
    while actions and actions[-1].tool == "wait":  # a delay before nothing
        actions.pop()
    reply = " ".join(str(data.get("reply") or "").split())
    return Intent(language, actions, reply)


def whole_seconds(value: Any) -> int | None:
    """A model's duration as whole seconds: 10, 10.0 and "10" are all 10; anything else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value == value and abs(value) < 1e9:
        return int(value) if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"\s*\d{1,9}(?:\.\d+)?\s*", value):
        return int(float(value))
    return None


# -- spoken descriptions (code, not model) --------------------------------------
_ISO = re.compile(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2})(?::(\d{2}))?)?")


def parse_local(value: Any, need_time: bool = False) -> datetime | None:
    """A model's local ISO value; seconds and any offset are dropped. "2026-09-28T7:00" is
    accepted too. ``need_time``: a bare date is refused (an alarm "2026-09-29" isn't midnight)."""
    if not isinstance(value, str):
        return None
    m = _ISO.match(value)
    if m is None or need_time and m.group(4) is None:
        return None
    y, mo, d, h, mi = (int(g) if g else 0 for g in m.groups())
    try:
        return datetime(y, mo, d, h, mi)
    except ValueError:
        return None


def day_phrase(d: date, today: date, hindi: bool) -> str:
    delta = (d - today).days
    if hindi:
        return {0: "आज", 1: "कल", 2: "परसों"}.get(delta, d.strftime("%d/%m"))
    return {0: "today", 1: "tomorrow"}.get(delta, d.strftime("on %A %d %B"))


def clock_phrase(t: datetime, hindi: bool) -> str:
    if not hindi:
        return t.strftime("%I:%M %p").lstrip("0")
    h = t.hour
    part = "सुबह" if 4 <= h < 12 else "दोपहर" if h < 16 else "शाम" if h < 20 else "रात"
    return f"{part} {h % 12 or 12}:{t.minute:02d} बजे"


def wait_phrase(secs: int, hindi: bool) -> str:
    """90 -> '1 minute 30 seconds' / '1 मिनट 30 सेकंड'."""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if hindi:
        units = [(h, "घंटे"), (m, "मिनट"), (s, "सेकंड")]
        return " ".join(f"{n} {u}" for n, u in units if n) or "0 सेकंड"
    units_en = [(h, "hour"), (m, "minute"), (s, "second")]
    return " ".join(f"{n} {u}{'s' if n != 1 else ''}" for n, u in units_en if n) or "0 seconds"


def describe(a: Action, language: str, now: datetime) -> str:
    hi = language != "en"
    g = a.args
    text = lambda k: str(g.get(k) or "").strip()
    if a.tool == "alarm.set" and (t := parse_local(g.get("time"), need_time=True)):
        return (f"{day_phrase(t.date(), now.date(), True)} {clock_phrase(t, True)} का अलार्म" if hi
                else f"an alarm for {clock_phrase(t, False)} {day_phrase(t.date(), now.date(), False)}")
    if a.tool == "timer.set" and (secs := whole_seconds(g.get("seconds"))):
        if secs % 60 == 0 and secs < 3600 or secs < 60:
            amount = f"{secs // 60} मिनट" if hi and secs >= 60 else f"{secs} सेकंड" if hi else (
                f"{secs // 60}-minute" if secs >= 60 else f"{secs}-second")
            return f"{amount} का टाइमर" if hi else f"a {amount} timer"
        # 90 s is "1 minute 30 seconds", not a "1-minute" timer
        return f"{wait_phrase(secs, True)} का टाइमर" if hi else f"a timer for {wait_phrase(secs, False)}"
    if a.tool == "calendar.create" and (t := parse_local(g.get("start"), need_time=True)):
        title = text("title") or ("इवेंट" if hi else "event")
        return (f"{day_phrase(t.date(), now.date(), True)} {clock_phrase(t, True)} “{title}”" if hi
                else f"“{title}” {day_phrase(t.date(), now.date(), False)} at {clock_phrase(t, False)}")
    if a.tool == "calendar.list":
        d = parse_local(f"{text('date')}T00:00")
        day = day_phrase(d.date(), now.date(), hi) if d else ("आज" if hi else "today")
        return f"{day} के इवेंट" if hi else f"your events {day}"
    if a.tool == "notes.add" and text("text"):
        return f"नोट: “{text('text')}”" if hi else f"a note: “{text('text')}”"
    if a.tool == "notes.search" and text("query"):
        return f"नोट्स में “{text('query')}” ढूँढना" if hi else f"a notes search for “{text('query')}”"
    if a.tool == "alarm.cancel":
        if t := parse_local(g.get("time"), need_time=True):
            return f"{clock_phrase(t, True)} का अलार्म रद्द करना" if hi else f"cancelling the {clock_phrase(t, False)} alarm"
        kind = str(g.get("kind") or "").lower()
        if kind == "timer":
            return "टाइमर रद्द करना" if hi else "cancelling your timer"
        if kind == "alarm":
            return "अलार्म रद्द करना" if hi else "cancelling your alarms"
        return "अलार्म और टाइमर रद्द करना" if hi else "cancelling your alarms and timers"
    if a.tool == "calendar.delete" and text("title"):
        return f"“{text('title')}” हटाना" if hi else f"deleting “{text('title')}”"
    if a.tool == "notes.delete" and text("query"):
        return f"“{text('query')}” वाला नोट हटाना" if hi else f"deleting the note about “{text('query')}”"
    if a.tool == "memory.remember" and text("text"):
        return f"याद रखना: “{text('text')}”" if hi else f"remembering “{text('text')}”"
    if a.tool == "memory.forget" and text("query"):
        return f"“{text('query')}” वाली बात भूलना" if hi else f"forgetting “{text('query')}”"
    if a.tool == "history.search":
        return "पिछली बातचीत में ढूँढना" if hi else "searching our past conversations"
    if a.tool == "app.open" and text("name"):
        return f"{text('name')} खोलना" if hi else f"opening {text('name')}"
    if a.tool == "app.close" and text("name"):
        return f"{text('name')} बंद करना" if hi else f"closing {text('name')}"
    if a.tool == "folder.open" and text("name"):
        return f"{text('name')} फ़ोल्डर खोलना" if hi else f"opening the {text('name')} folder"
    if a.tool == "web.open" and text("target"):
        return f"{text('target')} खोलना" if hi else f"opening {text('target')}"
    if a.tool == "settings.open" and text("page"):
        return f"{text('page')} सेटिंग्स खोलना" if hi else f"opening {text('page')} settings"
    if a.tool == "text.type" and text("text"):
        return f"टाइप करना: “{text('text')}”" if hi else f"typing “{text('text')}”"
    if a.tool == "ai.ask" and text("prompt"):
        who = "ChatGPT" if "gpt" in text("service").lower() else "Claude"
        return f"{who} से पूछना: “{text('prompt')}”" if hi else f"asking {who}: “{text('prompt')}”"
    if a.tool in ("files.recent", "files.reveal", "files.trash"):
        kind = text("kind") if text("kind") not in ("", "any") else ("फ़ाइल" if hi else "file")
        verb = {"files.recent": ("ढूँढना", "finding your latest"), "files.reveal": ("Finder में दिखाना", "showing your latest"),
                "files.trash": ("ट्रैश में डालना", "trashing your latest")}[a.tool]
        if not any(text(k) for k in ("kind", "when", "folder", "query")) and a.tool != "files.recent":
            return ("वह फ़ाइल " + verb[0]) if hi else verb[1].replace("your latest", "that file")
        return f"{kind} {verb[0]}" if hi else f"{verb[1]} {kind}"
    if a.tool == "wait" and (secs := whole_seconds(g.get("seconds"))) is not None:
        return f"{wait_phrase(secs, hi)} रुकना" if hi else f"waiting {wait_phrase(secs, False)}"
    if a.tool == "mac.do" and text("task"):
        return f"“{text('task')}”" if hi else f"doing “{text('task')}”"
    if a.tool == "email.unread":
        return "नए ईमेल देखना" if hi else "checking your unread email"
    if a.tool == "email.summary":
        return "ईमेल का सारांश" if hi else "summarising your email"
    if a.tool == "email.read":
        return (f"{text('from')} का ईमेल पढ़ना" if hi else f"reading the email from {text('from')}") if text("from") else (
            "ईमेल पढ़ना" if hi else "reading your latest email")
    if a.tool in ("email.draft", "email.send"):
        to = text("to") or ("उन्हें" if hi else "them")
        if a.tool == "email.draft":
            return f"{to} के लिए ड्राफ़्ट" if hi else f"a draft to {to}"
        return f"{to} को ईमेल भेजना" if hi else f"sending an email to {to}"
    if a.tool.startswith("drive."):
        q = text("query")
        verb = {"drive.search": ("Drive में ढूँढना", "searching your Drive"), "drive.recent": ("Drive की नई फ़ाइलें", "your latest Drive files"),
                "drive.summarize": ("Drive फ़ाइल का सारांश", "summarising a Drive file"), "drive.open": ("Drive फ़ाइल खोलना", "opening a Drive file")}[a.tool]
        return (verb[0] + (f": “{q}”" if q else "")) if hi else (verb[1] + (f": “{q}”" if q else ""))
    if a.tool == "files.search" and text("query"):
        return f"फ़ाइलों में “{text('query')}” ढूँढना" if hi else f"a file search for “{text('query')}”"
    generic = {"alarm.set": "अलार्म", "timer.set": "टाइमर", "calendar.create": "कैलेंडर इवेंट"}
    return generic.get(a.tool, a.tool) if hi else TOOLS[a.tool][0].lower()


def _join(parts: list[str], hindi: bool) -> str:
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + (" और " if hindi else " and ") + parts[-1]


def compose_reply(intent: Intent, gender: str) -> str:
    """Final spoken reply. For action requests it is built here, not by the
    model, so nothing is ever reported as done before tools exist."""
    if not intent.actions:
        return intent.reply
    hi = intent.language != "en"
    what = _join([a.summary for a in intent.actions], hi)
    if hi:
        understood = "समझ गया" if gender == "male" else "समझ गई"
        can = "सकता" if gender == "male" else "सकती"
        return f"{understood}: {what}। लेकिन अभी मैं यह काम नहीं कर {can}, यह टूल्स वाले अगले अपडेट में आएगा।"
    return f"Understood: {what}. I can't carry out actions yet; that arrives with the tools update."
