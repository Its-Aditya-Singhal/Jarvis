"""Instant understanding of simple commands, without the language model.

Timers, alarms, opening apps, notes, "what time is it", sums and conversions
(see ``jarvis.tools.calc``) and similar requests
follow a handful of patterns in English and Hinglish (Devanagari is
transliterated first). When the WHOLE utterance matches, it becomes an
Intent in well under a millisecond; anything else, or anything ambiguous,
returns None and goes to the LLM as before. Compound requests joined by
"and"/"aur"/"then" are handled if every part matches.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta

from ..speech.text import to_latin
from ..tools import calc
from ..tools.mac import FOLDERS, SITES, settings_page
from .intents import Action, Intent, clock_phrase, day_phrase, describe

NUM = {
    "zero": 0, "one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "twenty five": 25, "thirty": 30, "forty": 40, "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90,
    # Hindi (as spoken, and as transliterated from Devanagari)
    "ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4, "panch": 5, "paanch": 5, "pach": 5,
    "chhe": 6, "chhah": 6, "che": 6, "saat": 7, "sat": 7, "sath": 7, "saath": 7, "aath": 8, "ath": 8, "nau": 9, "das": 10,
    "gyarah": 11, "barah": 12, "baarah": 12, "pandrah": 15, "bees": 20, "bis": 20, "pachchis": 25,
    "tees": 30, "tis": 30, "chalis": 40, "paintalis": 45, "pachas": 50, "sadhe": None,
}
_NUMWORD = "|".join(sorted((k for k, v in NUM.items() if v is not None), key=len, reverse=True))
N = rf"(\d+|{_NUMWORD})"
N_ANY = rf"(?:\d+|{_NUMWORD})"

SEC = r"(?:seconds?|secs?|sekand|second)"
MIN = r"(?:minutes?|mins?|minat|minit|minute)"
HOUR = r"(?:hours?|hrs?|ghante?|ghanta|ghante)"
TIMER = r"(?:timer|taimar|taimer)"
ALARM = r"(?:alarm clock|alarm|alaram|alarma|alaarm|larm|rarm|alarm)"  # includes common mis-hearings
DO_IT = r"(?:laga|lagao|laga do|lagado|lagadu|laga du|laga dijiye|set kar do|set karo|set kr do|start karo|start kar do|chalu karo|chalao|kar do|karo)"
POLITE = r"(?:please |pls |zara |jara )?"
TAIL = r"(?: please| pls| na| jaldi)?"


def _num(s: str) -> int | None:
    s = s.strip()
    if s.isdigit():
        return int(s)
    return NUM.get(s)


def _clean(text: str) -> str:
    t = re.sub(r"^\s*(?:(?:hey|ok|okay|hi)[\s,]+)?jarvis\b[\s,:!.]*", "", text, flags=re.I)  # "Jarvis, open Safari"
    # a list "open notes, calendar and safari" (the comma is lost below): make it "…notes and calendar…"
    if re.match(r"\s*(?:please\s+)?(?:open|launch|start|close|quit)\b", t, re.I):
        t = re.sub(r",\s+(?!(?:and|then|aur|phir)\b)(?=[^\W\d])", " and ", t, flags=re.I)
    t = to_latin(t).replace("’", "'")
    t = re.sub(r"\b(\d+)\s*(am|pm)\b", r"\1 \2", t)
    t = re.sub(r"\b(a m|a\.m)\b", "am", t)
    t = re.sub(r"\b(p m|p\.m)\b", "pm", t)
    t = re.sub(r"^(hey |ok |okay |so |and |please |can you |could you |would you |will you |i want you to )+", "", t)
    return re.sub(r"\s+", " ", t).strip()


# -- durations ------------------------------------------------------------------------
def _duration(t: str) -> int | None:
    """'5 minutes', 'an hour and 10 minutes', 'half an hour', '90 sec', 'dedh ghanta'."""
    if re.fullmatch(r"(half an? hour|aadha ghanta|adha ghanta|aadhe ghante)", t):
        return 1800
    if re.fullmatch(r"(dedh|derh) ghant[ae]", t):
        return 5400
    total, rest = 0, t
    for unit, mult in ((HOUR, 3600), (MIN, 60), (SEC, 1)):
        m = re.match(rf"{N} {unit}\b(?: and | aur |, | )?", rest)
        if m:
            n = _num(m.group(1))
            if n is None:
                return None
            total += n * mult
            rest = rest[m.end():]
    return total if total and not rest.strip() else None


# -- clock times ----------------------------------------------------------------------
_PART = {"subah": "am", "subaha": "am", "subeh": "am", "subahe": "am", "savere": "am", "morning": "am", "dopahar": "pm", "afternoon": "pm",
         "shaam": "pm", "sham": "pm", "evening": "pm", "raat": "pm", "rat": "pm", "night": "pm", "tonight": "pm"}


def _clock(t: str, now: datetime, wake: bool) -> datetime | None:
    """'7', '7:30', '7 30 am', 'tomorrow at 6', 'kal subah 7 baje', 'saade saat baje'."""
    day = None
    words = t
    m = re.search(r"\b(tomorrow|kal|today|aaj|tonight)\b", words)
    if m:
        day = 1 if m.group(1) in ("tomorrow", "kal") else 0
        words = (words[: m.start()] + words[m.end():]).strip()
    part = None
    for k, v in _PART.items():
        if re.search(rf"\b{k}\b", words):
            part = v
            words = re.sub(rf"\b(in the )?{k}\b", "", words).strip()
    half = bool(re.search(r"\b(saade|sade|sadhe)\b", words))
    words = re.sub(r"\b(saade|sade|sadhe|at|for|ko|ke|ka|kaa|kar|baje|bajay|bje|o clock|oclock)\b", " ", words)
    words = re.sub(r"\s+", " ", words).strip()
    m = re.fullmatch(rf"{N}(?:[: ](\d\d))? ?(am|pm)?", words)
    if not m:
        return None
    h = _num(m.group(1))
    if h is None or not 0 <= h <= 23:
        return None
    minute = int(m.group(2)) if m.group(2) else (30 if half else 0)
    if minute > 59:
        return None
    ampm = m.group(3) or part
    if ampm and h > 12:
        return None
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    base = now.replace(second=0, microsecond=0)
    if day is not None:
        cands = [base.replace(hour=h, minute=minute) + timedelta(days=day)]
        if not ampm and h <= 12 and not wake:
            cands.append(cands[0].replace(hour=(h + 12) % 24))
    else:
        cands = []
        hours = [h] if ampm or h > 12 else ([h] if wake else [h, (h + 12) % 24])
        for d in (0, 1):
            for hh in hours:
                cands.append(base.replace(hour=hh, minute=minute) + timedelta(days=d))
    future = sorted(c for c in cands if c > now)
    return future[0] if future else None


# -- files: "the PDF I downloaded yesterday" ---------------------------------------------
_FILE_KIND = {
    "pdf": "pdf", "pdfs": "pdf", "screenshot": "screenshot", "screenshots": "screenshot", "screen shot": "screenshot",
    "image": "image", "images": "image", "photo": "image", "photos": "image", "picture": "image", "pictures": "image",
    "document": "document", "documents": "document", "doc": "document", "docs": "document",
    "spreadsheet": "spreadsheet", "spreadsheets": "spreadsheet", "excel file": "spreadsheet", "excel sheet": "spreadsheet",
    "presentation": "presentation", "presentations": "presentation", "slides": "presentation", "deck": "presentation",
    "video": "video", "videos": "video", "zip": "archive", "zips": "archive", "zip file": "archive", "dmg": "archive",
    "file": "any", "files": "any", "download": "any", "downloads": "any", "thing": "any",
}
_KIND_RE = "(" + "|".join(sorted(_FILE_KIND, key=len, reverse=True)) + ")"
_WHEN_RE = r"(today|yesterday|this week|last week|this month|this morning|last night|aaj|kal)"
_WHEN_ARG = {"aaj": "today", "kal": "yesterday", "this morning": "today", "last night": "yesterday"}
_PLACE_RE = r"(downloads|desktop|documents)"


def _file_ref(t: str) -> dict | None:
    """'the latest pdf i downloaded yesterday' -> {kind: pdf, when: yesterday, folder: downloads}.
    Needs a sign it means a particular recent file (latest, a day, "I downloaded"), so plain
    "my documents" stays the folder."""
    m = re.fullmatch(
        rf"(?:the |my |that |a )?(?:(latest|last|newest|most recent|recent|new) )?{_KIND_RE}"
        rf"(?: (?:that |which )?i (downloaded|saved|took|made|created|got|received|captured))?"
        rf"(?: (?:from |on )?{_WHEN_RE})?(?: (?:in|from|on) (?:my |the )?{_PLACE_RE})?(?: (?:from |on )?{_WHEN_RE})?", t)
    if not m:
        m2 = re.fullmatch(rf"(?:{_WHEN_RE} )(?:download|save)(?: ki| kiya| kari| kii)?(?: hui| hua| huyi| gayi| gaya)? {_KIND_RE}", t)
        if not m2:
            return None
        return {"kind": _FILE_KIND[m2.group(2)], "when": _WHEN_ARG.get(m2.group(1), m2.group(1)), "folder": "downloads"}
    latest, noun, verb, when1, place, when2 = m.groups()
    when = when1 or when2
    if not (latest or verb or when or place and noun not in ("download", "downloads")):
        return None
    args: dict = {"kind": _FILE_KIND[noun]}
    if when:
        args["when"] = _WHEN_ARG.get(when, when)
    if noun in ("download", "downloads") or verb == "downloaded":
        args["folder"] = "downloads"
    if place:
        args["folder"] = place
    return args


def _files(t: str) -> Parsed | None:
    IT = r"(?:it|that|this|that file|this file|the file|ise|isse|use|usko|ye file|yeh file|woh file|wo file)"
    if re.fullmatch(rf"{POLITE}(?:show|reveal|open|find)(?: me)? {IT} in (?:the )?finder|reveal {IT}|{IT} finder (?:mein|me) (?:dikhao|kholo)", t):
        return [Action("files.reveal", {})], ""
    if re.fullmatch(rf"{POLITE}(?:(?:move|put|send|throw) {IT} (?:to|in|into) (?:the )?(?:trash|bin|recycle bin)|trash {IT}|bin {IT})"
                    rf"|{IT} (?:ko )?(?:trash|bin) (?:mein|me) (?:daal do|dalo|daalo|daal|bhej do|dal do)", t):
        return [Action("files.trash", {})], ""
    m = re.fullmatch(rf"{POLITE}(?:show|reveal|find|open)(?: me)? (.+?) in (?:the )?finder", t)
    if m and (ref := _file_ref(m.group(1))):
        return [Action("files.reveal", ref)], ""
    m = re.fullmatch(rf"{POLITE}(?:(?:move|put|send|throw) (.+?) (?:to|in|into) (?:the )?(?:trash|bin)|(?:trash|delete) (.+))", t)
    if m and (ref := _file_ref(m.group(1) or m.group(2))):
        return [Action("files.trash", ref)], ""
    m = re.fullmatch(rf"what did i (download|save)(?: (?:from )?{_WHEN_RE})?", t)
    if m:
        args = {"kind": "any", "folder": "downloads"} if m.group(1) == "download" else {"kind": "any"}
        if m.group(2):
            args["when"] = _WHEN_ARG.get(m.group(2), m.group(2))
        return [Action("files.recent", args)], ""
    m = (re.fullmatch(rf"{POLITE}(?:find|show(?: me)?|list|get|where(?:'s| is| are)|what(?:'s| is| are)|search for|look for|pull up)(?: me)? (.+?)", t)
         or re.fullmatch(r"(.+?)(?: (?:dikhao|dhundo|dhoondo|dikha do|batao|kahan hai|kaha hai))", t))
    if m and (ref := _file_ref(m.group(1))):
        return [Action("files.recent", ref)], ""
    return None


# -- single commands -------------------------------------------------------------------
Parsed = tuple[list[Action], str]  # actions, direct reply (for questions)

_LEVEL = {"full": 100, "max": 100, "maximum": 100, "highest": 100, "poori": 100, "puri": 100, "poora": 100, "pura": 100,
          "half": 50, "aadhi": 50, "adhi": 50, "min": 0, "minimum": 0, "lowest": 0}
_LEVEL_RE = "(" + "|".join(sorted(_LEVEL, key=len, reverse=True)) + ")"


def _level_word(t: str, what: str) -> int | None:
    """'full volume', 'volume to max', 'set brightness to half', 'awaaz poori kar do' -> 100 / 50 / 0."""
    m = (re.fullmatch(rf"{POLITE}(?:set |turn |put |make )?(?:the )?(?:{what}) (?:to |at |on |up to )?(?:the )?{_LEVEL_RE}(?: level)?"
                      rf"(?: (?:karo|kar do|kardo|kr do|please))?{TAIL}", t)
         or re.fullmatch(rf"{POLITE}(?:set |turn |put |make )?(?:it )?(?:to )?{_LEVEL_RE} (?:{what})(?: please)?", t))
    return _LEVEL[m.group(1)] if m else None


def _restore(orig: str, cap: str) -> str | None:
    """The captured words with their original spelling and case (None for
    Devanagari input: free text isn't guessed from a transliteration)."""
    if re.search(r"[\u0900-\u097F]", orig):
        return None
    words = [re.escape(w) for w in cap.split()]
    m = re.search(r"[\s,;:.!?-]*".join(words), orig, re.I) if words else None
    out = (m.group(0) if m else cap).strip(" .!?")
    return out[:1].upper() + out[1:] if out else None


def _one(t: str, now: datetime, is_app: Callable[[str], bool], hi: bool, orig: str = "") -> Parsed | None:
    if (r := _files(t)) is not None:
        return r
    # timers
    m = (re.fullmatch(rf"{POLITE}(?:set|start|put)(?: me)?(?: a| an)? {TIMER} (?:for |of )?(.+?){TAIL}", t)
         or re.fullmatch(rf"{POLITE}(?:set|start|put|make)(?: me)?(?: a| an)? (.+?) {TIMER}(?: on| going)?{TAIL}", t)
         or re.fullmatch(rf"{POLITE}{TIMER} (?:for |of )?(.+?){TAIL}", t)
         or re.fullmatch(rf"{POLITE}(.+?) (?:ka |ke liye |ki )?{TIMER}(?: {DO_IT})?{TAIL}", t))
    if m and (secs := _duration(m.group(1))):
        return [Action("timer.set", {"seconds": secs})], ""
    m = re.fullmatch(rf"{POLITE}(?:remind me|wake me(?: up)?) in (.+?){TAIL}", t)
    if m and (secs := _duration(m.group(1))):
        return [Action("timer.set", {"seconds": secs})], ""

    # alarms
    m = (re.fullmatch(rf"{POLITE}(?:set|put)(?: an| a| my)? {ALARM} (?:for |at )?(.+?){TAIL}", t)
         or re.fullmatch(rf"{POLITE}{ALARM} (?:for|at) (.+?){TAIL}", t)
         or re.fullmatch(rf"{POLITE}(wake me(?: up)?) (?:at )?(.+?){TAIL}", t)
         or re.fullmatch(rf"{POLITE}(.+?) (?:ka |ke liye |ki )?{ALARM}(?: {DO_IT})?{TAIL}", t)
         or re.fullmatch(rf"{POLITE}(.+?) (?:jaga dena|jagana|utha dena|uthana){TAIL}", t))
    if m:
        wake = m.group(1).startswith("wake") or "jaga" in t or "utha" in t
        when = m.group(m.lastindex or 0)
        if (at := _clock(when, now, wake=wake or "subah" in when)) is not None:
            return [Action("alarm.set", {"time": at.isoformat(timespec="minutes")})], ""
    if re.fullmatch(rf"{POLITE}(?:cancel|delete|remove|turn off|stop) (?:all )?(?:my |the )?(?:{ALARM}s?|{TIMER}s?)(?: and (?:{ALARM}s?|{TIMER}s?))?{TAIL}", t) \
            or re.fullmatch(rf"(?:saare |sab |mera |mere )?(?:{ALARM}|{TIMER}) (?:cancel|band|hata) (?:kar do|karo|do)", t):
        # "cancel the timer" must leave tomorrow's wake-up alarm alone
        alarm, timer = re.search(rf"\b{ALARM}", t) is not None, re.search(rf"\b{TIMER}", t) is not None
        return [Action("alarm.cancel", {"kind": "timer"} if timer and not alarm else {"kind": "alarm"} if alarm and not timer else {})], ""

    # web addresses lose their dots in _clean, so look for them in the original words
    if re.fullmatch(rf"{POLITE}(?:open|go to|visit|browse to) .+", t):
        url = re.search(r"\b((?:https?://)?[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?:/[^\s]*)?)", orig.lower())
        if url and url.group(1).replace(".", " ").replace("/", " ").split()[0] in t:
            return [Action("web.open", {"target": url.group(1).rstrip(".,!?")})], ""

    # apps, folders and websites ("open music" is the app, "open music folder" the folder)
    m = (re.fullmatch(rf"{POLITE}(?:open|launch|start|run|show(?: me)?|go to) (?:the |my )?(.+?)(?: app| application)?{TAIL}", t)
         or re.fullmatch(rf"{POLITE}(?:the |my |meri |mera )?(.+?) (?:kholo|khol do|kholdo|khol|open karo|open kar do|open kr do|chalu karo|start karo|dikhao){TAIL}", t))
    if m:
        name = m.group(1).strip()
        base = re.sub(r"\s+(?:folder|directory)$", "", name)
        if base != name and (base in FOLDERS or len(base.split()) <= 3):
            return [Action("folder.open", {"name": base})], ""
        if is_app(name):
            return [Action("app.open", {"name": name})], ""
        if name in FOLDERS:
            return [Action("folder.open", {"name": name})], ""
        if name in SITES or re.fullmatch(r"[\w-]+(\.[\w-]+)+(/\S*)?", name):
            return [Action("web.open", {"target": name})], ""
    m = (re.fullmatch(rf"{POLITE}(?:close|quit|exit|kill|shut down|shut) (?:the |my )?(.+?)(?: app| application)?{TAIL}", t)
         or re.fullmatch(rf"{POLITE}(?:the |my )?(.+?)(?: app)? (?:ko )?(?:band karo|band kar do|band kardo|band kr do|band krdo|bandh karo|band){TAIL}", t))
    if m and len(m.group(1).split()) <= 3 and is_app(m.group(1)) and not re.search(rf"\b(?:{ALARM}|{TIMER}|music|song|gaana|gana|volume|awaaz|sound)s?\b", m.group(1)):
        return [Action("app.close", {"name": m.group(1)})], ""

    # web search (explicit "google" / "web" / "online" only; "search for X" may mean files)
    m = (re.fullmatch(r"(?:google|search google for|search the web for|search online for|look up|search on google for|google search) (.+?)(?: online| on google)?", t)
         or re.fullmatch(r"(?:search|look up) (.+?) (?:on google|online|on the web|on the internet)", t)
         or re.fullmatch(r"(.+?) (?:google karo|google pe search karo|search karo google pe)", t))
    if m and (q := _restore(orig, m.group(1)) or m.group(1)):
        return [Action("web.open", {"target": q})], ""

    # volume
    VOL = r"(?:the )?(?:volume|sound|awaaz|aawaz|awaz)"
    if re.fullmatch(rf"{POLITE}(?:mute|mute (?:the )?(?:sound|volume|mac|audio))|{VOL} (?:mute|band) (?:karo|kar do)|chup(?: ho jao| raho)?", t):
        return [Action("system.volume", {"mute": True})], ""
    if re.fullmatch(rf"{POLITE}(?:unmute|unmute (?:the )?(?:sound|volume|mac|audio)|{VOL} (?:chalu|on) (?:karo|kar do)|turn (?:the )?(?:sound|volume) (?:back )?on)", t):
        return [Action("system.volume", {"mute": False})], ""
    m = re.fullmatch(rf"{POLITE}(?:set (?:the )?(?:volume|sound) (?:to|at)|volume|volume to) {N}(?: ?%| percent)?{TAIL}", t)
    if m and (n := _num(m.group(1))) is not None and 0 <= n <= 100:
        return [Action("system.volume", {"level": n})], ""
    if (n := _level_word(t, r"volume|sound|awaaz|aawaz|awaz")) is not None:
        return [Action("system.volume", {"level": n})], ""
    up = rf"(?:{POLITE}(?:turn|crank) (?:it|the volume|the sound|volume) up(?: a bit| a little)?|(?:increase|raise) {VOL}|{VOL} (?:up|badhao|badha do|tez karo|zyada karo)|louder|a bit louder)"
    down = rf"(?:{POLITE}turn (?:it|the volume|the sound|volume) down(?: a bit| a little)?|(?:decrease|lower|reduce) {VOL}|{VOL} (?:down|kam karo|kam kar do|dheere karo|ghatao)|(?:be )?quieter|(?:a bit )?softer)"
    if re.fullmatch(up, t):
        return [Action("system.volume", {"change": 10})], ""
    if re.fullmatch(down, t):
        return [Action("system.volume", {"change": -10})], ""
    if re.fullmatch(rf"what(?:'?s| is) (?:the )?(?:volume|sound level)(?: now| at)?|{VOL} kitni hai", t):
        return [Action("system.volume", {})], ""

    # music
    SONG = r"(?:music|song|songs|the song|the music|track|gaana|gana|gaane|gane|spotify)"
    if re.fullmatch(rf"{POLITE}(?:play|resume|start|continue)(?: (?:some |the |my )?{SONG})?|{SONG} (?:chalao|bajao|chala do|baja do|play karo|shuru karo)", t):
        return [Action("media.control", {"action": "play"})], ""
    if re.fullmatch(rf"{POLITE}(?:pause|stop)(?: (?:the )?{SONG})|pause|{SONG} (?:roko|rok do|band karo|band kar do|pause karo)", t):
        return [Action("media.control", {"action": "pause"})], ""
    if re.fullmatch(rf"{POLITE}(?:next|skip)(?: (?:this )?{SONG})?|(?:play )?(?:the )?next {SONG}|agla (?:gaana|gana)(?: chalao| lagao)?|skip karo|next karo", t):
        return [Action("media.control", {"action": "next"})], ""
    if re.fullmatch(rf"{POLITE}(?:previous|go back|last)(?: {SONG})?|(?:play )?(?:the )?previous {SONG}|pichla (?:gaana|gana)(?: chalao| lagao)?", t):
        return [Action("media.control", {"action": "previous"})], ""

    # battery, lock, alarm list
    if re.fullmatch(r"(?:what(?:'?s| is) (?:my |the )?)?battery(?: level| percentage| status| left)?|how much battery(?: do i have| is left| left| do we have)?|(?:kitni )?battery(?: kitni)?(?: hai| bachi hai)?", t):
        return [Action("system.battery", {})], ""
    if re.fullmatch(rf"{POLITE}lock (?:the |my )?(?:screen|mac|computer|laptop|it)(?: now)?|(?:screen|mac|laptop) lock (?:karo|kar do)|lock", t):
        return [Action("system.lock", {})], ""
    if re.fullmatch(rf"(?:what|which|any) (?:{ALARM}s?|{TIMER}s?)(?: (?:do i have|are set|are there|have i set))?|(?:show|list|tell me)(?: me)? (?:all )?(?:my |the )?(?:{ALARM}s?|{TIMER}s?)(?: and (?:{ALARM}s?|{TIMER}s?))?|how much time (?:is )?left(?: on (?:my |the )?{TIMER})?|(?:do i have|are there) any (?:{ALARM}s?|{TIMER}s?)(?: set)?|(?:kaunse|kitne) (?:{ALARM}|{TIMER}) (?:lage|set) hain", t):
        return [Action("alarm.list", {})], ""

    # screen & display
    SHOT = r"(?:a )?(?:screenshot|screen shot|screen grab|screengrab|screen capture)"
    if re.fullmatch(rf"{POLITE}(?:take|grab|capture|copy|get)(?: me)? {SHOT}(?: of (?:the |my )?(?:screen|display))?(?: to (?:the |my )?clipboard)?{TAIL}"
                    rf"|{SHOT}(?: to (?:the |my )?clipboard)?|{POLITE}capture (?:the |my )?(?:screen|display)"
                    rf"|(?:screen ka )?(?:screenshot|screen shot) (?:lo|le lo|lelo|le do|ledo|kheecho|khicho|khich lo|lena)", t):
        clip = "clipboard" in t or t.startswith("copy")
        return [Action("screen.shot", {"to": "clipboard"} if clip else {})], ""
    BRIGHT = r"(?:the )?(?:screen )?(?:brightness|brightnes|roshni)"
    m = re.fullmatch(rf"{POLITE}(?:set (?:the )?(?:screen )?brightness (?:to|at)|brightness(?: to)?) {N}(?: ?%| percent)?{TAIL}", t)
    if m and (n := _num(m.group(1))) is not None and 0 <= n <= 100:
        return [Action("display.brightness", {"level": n})], ""
    if re.fullmatch(rf"{POLITE}(?:turn|crank) (?:the )?brightness (?:all the way )?up (?:all the way|to (?:the )?max)", t):
        return [Action("display.brightness", {"level": 100})], ""
    if (n := _level_word(t, r"(?:screen )?brightness|roshni")) is not None:
        return [Action("display.brightness", {"level": n})], ""
    if re.fullmatch(rf"{POLITE}(?:turn (?:the )?brightness up(?: a bit| a little)?|(?:increase|raise) {BRIGHT}|{BRIGHT} (?:up|badhao|badha do|tez karo|zyada karo)"
                    rf"|make (?:the |my )?(?:screen|display) brighter|brighter(?: screen)?|screen (?:ko )?bright karo)", t):
        return [Action("display.brightness", {"change": 10})], ""
    if re.fullmatch(rf"{POLITE}(?:turn (?:the )?brightness down(?: a bit| a little)?|(?:decrease|lower|reduce|dim) {BRIGHT}|{BRIGHT} (?:down|kam karo|kam kar do|ghatao)"
                    rf"|dim (?:the |my )?(?:screen|display)(?: a bit| a little)?|make (?:the |my )?(?:screen|display) (?:dimmer|darker)|dimmer|screen (?:ko )?(?:dim|dark) karo)", t):
        return [Action("display.brightness", {"change": -10})], ""
    if re.fullmatch(rf"what(?:'?s| is) (?:the |my )?(?:screen )?brightness(?: level| at| now)?|{BRIGHT} kitni hai", t):
        return [Action("display.brightness", {})], ""
    DARK = r"(?:dark mode|dark theme|night mode|dark appearance)"
    LIGHT = r"(?:light mode|light theme|light appearance)"
    if re.fullmatch(rf"{POLITE}(?:turn on|enable|switch to|go to|use|activate|switch on) {DARK}|{DARK}(?: on)?|{DARK} (?:on|chalu) (?:karo|kar do)"
                    rf"|{POLITE}(?:turn off|disable|switch off) {LIGHT}", t):
        return [Action("display.dark_mode", {"on": True})], ""
    if re.fullmatch(rf"{POLITE}(?:turn off|disable|switch off|deactivate) {DARK}|{DARK} off|{DARK} (?:band|off) (?:karo|kar do)"
                    rf"|{POLITE}(?:turn on|enable|switch to|go to|use|activate) {LIGHT}|{LIGHT}(?: on)?|{LIGHT} (?:on|chalu) (?:karo|kar do)", t):
        return [Action("display.dark_mode", {"on": False})], ""
    if re.fullmatch(rf"{POLITE}(?:toggle|switch) (?:the )?(?:{DARK}|appearance|theme)", t):
        return [Action("display.dark_mode", {})], ""
    m = (re.fullmatch(rf"{POLITE}(?:open|show(?: me)?|go to|take me to) (?:the |my )?(.+?) (?:settings?|preferences|prefs)(?: page| pane)?{TAIL}", t)
         or re.fullmatch(rf"{POLITE}(?:open|show(?: me)?|go to) (?:the )?(?:system )?settings (?:for|of) (?:the |my )?(.+?){TAIL}", t)
         or re.fullmatch(rf"{POLITE}(?:the |my )?(.+?) (?:ki |ke |ka )?(?:settings?) (?:kholo|khol do|kholdo|open karo|open kar do|dikhao){TAIL}", t))
    if m and settings_page(m.group(1)):
        return [Action("settings.open", {"page": m.group(1)})], ""

    # clipboard & typing
    CLIP = r"(?:the |my )?(?:clipboard|clip board)"
    if re.fullmatch(rf"{POLITE}(?:save|add|put|turn|make) {CLIP}(?: text)? (?:as|to|into|in) (?:a |my )?notes?|{POLITE}(?:make|take|save) a note (?:of|from) {CLIP}"
                    rf"|{POLITE}save what i (?:just )?copied(?: as a note| to (?:my )?notes)|{CLIP} (?:ko )?note (?:mein |me )?(?:save karo|save kar do|bana do|daal do)", t):
        return [Action("clipboard.note", {})], ""
    if re.fullmatch(rf"what(?:'?s| is) (?:on|in) {CLIP}|{POLITE}(?:read|tell me|say)(?: me| out)? (?:what'?s (?:on|in) )?{CLIP}(?: out)?"
                    rf"|what did i (?:just )?copy|{POLITE}read (?:me )?what i (?:just )?copied|{CLIP} (?:mein|me) kya hai|{CLIP} (?:padho|padh do|batao)", t):
        return [Action("clipboard.read", {})], ""
    m = (re.fullmatch(r"(?:please )?(?:type out|type|dictate)(?: this| the following)?:? (.+)", t)
         or re.fullmatch(r"(.+?) (?:type karo|type kar do|type kardo|type kr do)", t))
    if m and (text := _restore(orig, m.group(1))):
        return [Action("text.type", {"text": text})], ""

    # notes
    m = re.fullmatch(rf"{POLITE}(?:take|add|make|write|save)(?: a| me a)? note(?: that| to| saying|:)? (.+)", t) \
        or re.fullmatch(r"note (?:down |that )(.+)", t)
    if m and len(m.group(1).split()) >= 2 and (text := _restore(orig, m.group(1))):
        return [Action("notes.add", {"text": text})], ""

    # memory
    m = (re.fullmatch(r"(?:please )?(?:remember|don'?t forget|do not forget|yaad rakh(?:na|o|iye)?)(?: that| this| ki| ke)?:? (?!to )(.+)", t)
         or re.fullmatch(r"(.+?),? (?:yaad rakhna|yaad rakho|yaad rakhiye|ye yaad rakhna|yeh yaad rakhna|remember that)", t))
    if m and len(m.group(1).split()) >= 2 and (text := _restore(orig, m.group(1))):
        return [Action("memory.remember", {"text": text})], ""
    m = re.fullmatch(r"(?:please )?forget (?:about |that |the fact that |what i said about )?(.+)", t)
    if m and (text := _restore(orig, m.group(1))):
        return [Action("memory.forget", {"query": text})], ""

    # calendar
    m = re.fullmatch(r"(?:what(?:'?s| is| do i have)(?: on)?(?: in)? my (?:calendar|schedule|agenda)|what do i have|what'?s my schedule|(?:my )?(?:calendar|schedule))(?: for)? ?(today|tomorrow)?", t) \
        or re.fullmatch(r"(aaj|kal) (?:ka |ke )?(?:schedule|calendar|plan)(?: kya hai| batao| dikhao)?", t)
    if m:
        d = now.date() + timedelta(days=1 if (m.group(1) or "") in ("tomorrow", "kal") else 0)
        return [Action("calendar.list", {"date": d.isoformat()})], ""

    # files
    m = re.fullmatch(r"(?:find|search(?: for)?|look for|show me)(?: my)? (?:files?|documents?|docs?) (?:about|named|called|for|with|on) (.+)", t)
    if m and (q := _restore(orig, m.group(1))):
        return [Action("files.search", {"query": q.lower()})], ""

    # time and date questions
    if re.fullmatch(r"(?:(?:tell me|say|do you know|you know|show me) )?(?:what(?:'?s| is) )?(?:the )?(?:current |exact )?time(?: is it)?(?: (?:right )?now| please| abhi| currently)?"
                    r"|what(?:'?s| is) the time(?: (?:right )?now)?|what time is it(?: (?:right )?now| currently)?|(?:tell me |say )?what time it is"
                    r"|time kya (?:hua|ho gaya|hai)(?: hai)?|(?:abhi )?kitne baje(?: hain| hai)?|kitna baja hai|samay kya hua hai|time batao|time bolo|abhi (?:ka )?time(?: kya hai)?", t):
        return [], (f"अभी {clock_phrase(now, True)} हैं।" if hi else f"It's {clock_phrase(now, False)}.")
    if re.fullmatch(r"(?:(?:tell me|say) )?(?:what(?:'?s| is) (?:the )?(?:date|day)(?: today)?|what day is (?:it|today)|which day is (?:it|today)|what(?:'?s| is) today'?s date|today'?s date|(?:the )?date(?: today)?"
                    r"|what date is it(?: today)?|aaj (?:kya )?(?:date|tareekh|tarikh|din) (?:kya )?hai|aaj kaun sa din hai|aaj ki (?:date|tareekh|tarikh)(?: kya hai| batao)?)", t):
        if hi:
            return [], f"आज {now.strftime('%A, %d %B %Y')} है।"
        return [], f"Today is {now.strftime('%A, %B')} {now.day}, {now.year}."
    return None


# "ask Claude to …" / "open ChatGPT and ask it …": the rest is the prompt, "and" included
_AI = r"(claude|claud|cloud|chat ?gpt|chat ?gbt)"
_ASK = [
    rf"(?:please )?(?:open|launch|go to|use) {_AI}(?: app)?,? (?:and |then )?(?:ask|tell)(?: it| him| her)?(?: to| for| about| that| ki)?:? (.+)",
    rf"(?:please )?(?:ask|tell) {_AI}(?: to| for| about| that| ki)?:? (.+)",
    rf"(?:please )?use {_AI} to (.+)",
    rf"{_AI} (?:se|ko) (?:pucho|poocho|puchho|poochho|bolo|kaho)(?: ki)? (.+)",
]
_ASK_TAIL = rf"(.+?),? (?:ye |yeh )?{_AI} (?:se|ko) (?:pucho|poocho|puchho|poochho|bolo|kaho)"


def _ask_ai(t: str, orig: str) -> Action | None:
    for pat in _ASK:
        if m := re.fullmatch(pat, t):
            who, what = m.group(1), m.group(2)
            break
    else:
        if not (m := re.fullmatch(_ASK_TAIL, t)):
            return None
        who, what = m.group(2), m.group(1)
    prompt = _restore(orig, what)
    if not prompt or len(what.split()) < 2:
        return None
    return Action("ai.ask", {"service": "chatgpt" if "g" in who.replace("cloud", "") else "claude", "prompt": prompt})


# "close it after 10 seconds", "in 5 minutes open Safari", "10 second baad band karo"
_DUR = rf"((?:{N_ANY}|half an?|an?) (?:{HOUR}|{MIN}|{SEC})(?:(?: and | aur |, | ){N_ANY} (?:{MIN}|{SEC}))?)"
_DELAY = [
    (rf"(.+?),? (?:after|in|within) {_DUR}(?: from now)?", 1, 2),
    (rf"(?:after|in|wait) {_DUR},? (?:and |then )?(.+)", 2, 1),
    (rf"{_DUR} (?:ke )?baad (.+)", 2, 1),
    (rf"(.+?) {_DUR} (?:ke )?baad", 1, 2),
]
MAX_WAIT_S = 6 * 3600
_IT = r"(?:it|that|this|that app|this app|the app|ise|isko|usko|use|usse|isse|woh|wo|ye|yeh)"
_OPEN = r"(?:open|launch|start|run|close|quit|exit|kill|shut down|shut)"


def _delayed(p: str) -> tuple[int, str] | None:
    """A part that is 'X after <duration>' -> (seconds, X)."""
    m = re.fullmatch(rf"(.+?) {_DUR} (?:ke )?baad (.+)", p)  # "whatsapp 10 second baad band karo"
    if m and (secs := _duration(m.group(2))) and secs <= MAX_WAIT_S:
        return secs, f"{m.group(1)} {m.group(3)}"
    for pat, what, dur in _DELAY:
        m = re.fullmatch(pat, p)
        if m and (secs := _duration(m.group(dur).strip())) and secs <= MAX_WAIT_S:
            rest = m.group(what).strip()
            if rest and not re.fullmatch(rf"{POLITE}(?:remind me|wake me(?: up)?|set (?:a |an )?(?:{TIMER}|{ALARM}))(?: .*)?", rest):
                return secs, rest
    return None


def _resolve_it(p: str, last: str | None) -> str:
    """'close it' -> 'close whatsapp' when an app was just named ('open whatsapp and close it')."""
    if not last:
        return p
    p = re.sub(rf"^({POLITE}{_OPEN}) {_IT}\b", rf"\1 {last}", p)
    return re.sub(rf"^{_IT}(?: app)? (?=(?:ko )?(?:band|bandh|kholo|khol|open|close)\b)", f"{last} ", p)


def parse_fast(text: str, language: str, now: datetime, is_app: Callable[[str], bool] = lambda n: False,
               last_app: str | None = None) -> Intent | None:
    """``last_app``: the app the previous command opened or closed ("close it" refers to it)."""
    t = _clean(text)
    if not t:
        return None
    hi = language != "en"
    if len(t) <= 800 and (ask := _ask_ai(t, text)) is not None:
        ask.summary = describe(ask, language, now)
        return Intent(language, [ask], "")
    if len(t) > 160:
        return None
    # sums, conversions and date math are checked on the whole request ("add 5 and 3" is one question)
    if (said := calc.answer(text, now, hi)) is not None:
        return Intent(language, [], said)
    # "and" inside a duration ("an hour and 10 minutes") is not a split point
    # nor is it in "alarms and timers"
    joiner = rf"\s*(?:,? and then|,? and|,? then|,? aur phir|,? aur|,? phir)\s+(?!{N_ANY} (?:{MIN}|{SEC})\b|(?:{ALARM}|{TIMER})s?\b)"
    parts = [p.strip() for p in re.split(joiner, t) if p.strip()]
    actions: list[Action] = []
    reply = ""
    verb = ""  # "open notes and calendar": the bare "calendar" borrows "open"
    for p in parts:
        r = _part(p, now, is_app, hi, text, last_app, verb)
        if r is None:
            return None  # any part the patterns don't cover -> the LLM handles the whole request
        acts, say = r
        if say and (reply or len(parts) > 1):
            return None
        actions += acts
        reply = say
        for a in acts:
            if a.tool in ("app.open", "app.close"):
                last_app = str(a.args.get("name") or "") or last_app
        if (v := re.match(rf"{POLITE}({_OPEN})\b", p)) is not None:
            verb = v.group(1)
    for a in actions:
        a.summary = describe(a, language, now)
    return Intent(language, actions, reply)


def _part(p: str, now: datetime, is_app: Callable[[str], bool], hi: bool, orig: str, last_app: str | None,
          verb: str) -> Parsed | None:
    p = _resolve_it(p, last_app)
    if verb and (is_app(p) or p in FOLDERS or p in SITES) and (r := _one(f"{verb} {p}", now, is_app, hi, orig)):
        return r
    if (r := _one(p, now, is_app, hi, orig)) is not None:
        return r
    if (d := _delayed(p)) is not None:
        secs, rest = d
        r = _part(rest, now, is_app, hi, orig, last_app, verb)
        if r is not None and r[0] and not r[1]:
            return [Action("wait", {"seconds": secs}), *r[0]], ""
    return None


__all__ = ["parse_fast", "day_phrase"]
