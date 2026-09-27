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
from ..tools.mac import FOLDERS, SITES
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
    t = to_latin(text).replace("’", "'")
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


# -- single commands -------------------------------------------------------------------
Parsed = tuple[list[Action], str]  # actions, direct reply (for questions)


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
    # timers
    m = (re.fullmatch(rf"{POLITE}(?:set|start|put)(?: me)?(?: a| an)? {TIMER} (?:for |of )?(.+?){TAIL}", t)
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
        return [Action("alarm.cancel", {})], ""

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


def parse_fast(text: str, language: str, now: datetime, is_app: Callable[[str], bool] = lambda n: False) -> Intent | None:
    t = _clean(text)
    if not t or len(t) > 160:
        return None
    hi = language != "en"
    # sums, conversions and date math are checked on the whole request ("add 5 and 3" is one question)
    if (said := calc.answer(text, now, hi)) is not None:
        return Intent(language, [], said)
    # "and" inside a duration ("an hour and 10 minutes") is not a split point
    joiner = rf"\s*(?:,? and then|,? and|,? then|,? aur phir|,? aur|,? phir)\s+(?!{N_ANY} (?:{MIN}|{SEC})\b)"
    parts = [p.strip() for p in re.split(joiner, t) if p.strip()]
    actions: list[Action] = []
    reply = ""
    for p in parts:
        r = _one(p, now, is_app, hi, text)
        if r is None:
            return None  # any part the patterns don't cover -> the LLM handles the whole request
        acts, say = r
        if say and (reply or len(parts) > 1):
            return None
        actions += acts
        reply = say
    for a in actions:
        a.summary = describe(a, language, now)
    return Intent(language, actions, reply)


__all__ = ["parse_fast", "day_phrase"]
