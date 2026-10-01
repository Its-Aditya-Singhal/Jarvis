"""Gmail, Google Drive and Google Calendar as assistant tools.

Every tool reads only what the request needs (ten mail headers and snippets for a summary, one
mail's body, one document's text) and, when it needs words written, makes one request to the
writing model (``Brain.write`` / ``write_json``). Sending is never direct: ``plan_send`` saves
or reuses a Gmail draft and returns a Plan whose question reads the whole mail back; the
service sends it only after a voice-verified "yes" or a click (level 3).

The latest mail read and the latest draft are kept in memory so follow-ups work: "what did
Rahul mail me" → "okay, send him a mail confirming I'll come" → "send it".
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ..google.auth import GoogleAuth, GoogleError
from ..google.drive import Drive, DriveFile
from ..google.gcal import GoogleCalendar
from ..google.gmail import EMAIL_RE, Gmail, Mail
from ..llm.client import LLMUnavailable
from ..llm.intents import clock_phrase, day_phrase

log = logging.getLogger(__name__)

CONTEXT_TTL_S = 30 * 60.0  # "him", "that mail", "send it" refer to things from this long ago at most
PRONOUNS = {"him", "her", "them", "he", "she", "they", "usko", "use", "unko", "unhe", "inhe", "isko", "that person",
            "the sender", "sender", "my friend", "that guy", "same person", "back", "reply"}
MAX_SUMMARY = 25


class Writer(Protocol):
    def write(self, system: str, text: str, max_tokens: int = 700) -> str: ...

    def write_json(self, system: str, text: str, max_tokens: int = 700) -> dict: ...


@dataclass
class Draft:
    id: str
    to_name: str
    to_addr: str
    subject: str
    body: str
    reply: Mail | None
    t: float

    def text(self) -> str:
        return f"To: {self.to_name} <{self.to_addr}>\nSubject: {self.subject}\n\n{self.body}"


@dataclass
class Result:
    """What a Google tool did: the runner turns it into a ToolResult."""

    ok: bool
    say: str
    data: dict[str, Any]


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _sentence(s: str, hi: bool) -> str:
    s = s.strip()
    return s if not s or s[-1] in ".!?।" else s + ("।" if hi else ".")


def _lang(hi: bool) -> str:
    return ("Write in Hindi (Devanagari script)." if hi else "Write in English.")


class GoogleTools:
    def __init__(self, auth: GoogleAuth, names: Callable[[], tuple[str, str]] = lambda: ("JARVIS", ""),
                 open_url: Callable[[str], None] = lambda url: None, clock: Callable[[], datetime] = datetime.now):
        self.auth = auth
        self.gmail = Gmail(auth)
        self.drive = Drive(auth)
        self.calendar = GoogleCalendar(auth)
        self.names = names
        self.open_url = open_url
        self.clock = clock
        self.writer: Writer | None = None  # the brain (set when the language model is on)
        self.last_mail: tuple[Mail, float] | None = None
        self.last_draft: Draft | None = None
        self.last_files: list[DriveFile] = []

    # -- helpers ---------------------------------------------------------------------------
    def run(self, tool: str, args: dict, hi: bool) -> Result:
        fn = getattr(self, tool.replace(".", "_"), None)
        if fn is None:
            return Result(False, "That tool isn't available.", {})
        try:
            return fn(args, hi)
        except GoogleError as exc:
            return Result(False, _sentence(str(exc)[:1].upper() + str(exc)[1:], False), {"google_error": True})

    def _write(self, system: str, text: str, max_tokens: int = 500) -> str | None:
        if self.writer is None:
            return None
        try:
            return self.writer.write(system, text, max_tokens).strip() or None
        except (LLMUnavailable, ValueError) as exc:
            log.warning("writing model unavailable: %s", exc)
            return None

    def _mail_ctx(self) -> Mail | None:
        m = self.last_mail
        return m[0] if m is not None and time.monotonic() - m[1] < CONTEXT_TTL_S else None

    def _when(self, d: datetime | None, hi: bool) -> str:
        if d is None:
            return ""
        now = self.clock()
        return (f"{day_phrase(d.date(), now.date(), True)} {clock_phrase(d, True)}" if hi
                else f"{day_phrase(d.date(), now.date(), False)} at {clock_phrase(d, False)}")

    @staticmethod
    def _count(v: Any, default: int) -> int:
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            return default
        return max(1, min(n, MAX_SUMMARY))

    @staticmethod
    def _query(args: dict) -> str:
        """The owner's search words as a Gmail query: "from:" for a person, the rest as words."""
        q = " ".join(str(args.get("query") or "").split())[:120]
        who = " ".join(str(args.get("from") or "").split())[:60]
        if who and who.lower() not in PRONOUNS:
            q = (f"from:{who}" if EMAIL_RE.match(who) or " " not in who else f'from:"{who}"') + (f" {q}" if q else "")
        return q

    # -- mail -------------------------------------------------------------------------------
    def email_unread(self, args: dict, hi: bool) -> Result:
        n, more, mails = self.gmail.unread(10)
        if not n:
            return Result(True, "कोई नया ईमेल नहीं है।" if hi else "No unread email in your inbox.", {"count": 0, "mails": []})
        senders = list(dict.fromkeys(m.sender for m in mails))[:3]
        count = f"{n}+" if more else str(n)
        who = ", ".join(senders[:-1]) + (" और " if hi else " and ") + senders[-1] if len(senders) > 1 else senders[0]
        say = (f"{count} नए ईमेल हैं, {who} से।" if hi else
               f"You have {count} unread email{'s' if n != 1 else ''}, from {who}"
               + (" and others." if n > len(senders) else "."))
        return Result(True, say, {"count": n, "more": more, "mails": [m.public() for m in mails]})

    def email_summary(self, args: dict, hi: bool) -> Result:
        n = self._count(args.get("count"), 10)
        q = self._query(args)
        mails = self.gmail.search(f"in:inbox {q}".strip(), n)
        data = {"mails": [m.public() for m in mails], "query": q}
        if not mails:
            return Result(True, "ऐसा कोई ईमेल नहीं मिला।" if hi else "I didn't find any matching email.", data)
        lines = [f"{i + 1}. From {m.sender}; {'unread; ' if m.unread else ''}"
                 f"{m.date.strftime('%a %d %b %H:%M') + '; ' if m.date else ''}subject: {m.subject}; preview: {_clip(m.snippet, 220)}"
                 for i, m in enumerate(mails)]
        assistant, owner = self.names()
        text = self._write(
            f"You are {assistant}, {owner}'s voice assistant. Summarise these {len(mails)} emails for {owner}, spoken "
            "aloud: at most 6 short sentences, no lists, markdown or emoji. Group routine mail (newsletters, "
            "notifications, promotions) in one sentence; name people and what they want; say what looks urgent or "
            f"needs a reply first. {_lang(hi)}", "\n".join(lines), 450)
        if text is None:  # no AI right now: still useful
            senders = list(dict.fromkeys(m.sender for m in mails))[:5]
            text = (f"{len(mails)} ईमेल: {', '.join(senders)} से।" if hi else
                    f"Your last {len(mails)} emails are from {', '.join(senders)}. The newest: "
                    f"{_clip(mails[0].subject, 80)}. (I can't summarise them right now.)")
        self.last_mail = (mails[0], time.monotonic())
        return Result(True, text, data)

    def email_read(self, args: dict, hi: bool) -> Result:
        q = self._query(args)
        who = str(args.get("from") or "").strip().lower()
        ctx = self._mail_ctx()
        if not q and who in PRONOUNS and ctx is not None:
            q = f"from:{ctx.sender_addr}"
        ids = self.gmail.ids(f"in:inbox {q}".strip(), 1)[0]
        if not ids:
            return Result(False, "ऐसा कोई ईमेल नहीं मिला।" if hi else "I didn't find an email like that.", {"query": q})
        m = self.gmail.message(ids[0], full=True)
        self.last_mail = (m, time.monotonic())
        when = self._when(m.date, hi)
        data = {"mails": [m.public()], "body": m.body[:4000]}
        body = m.body or m.snippet
        if len(body) <= 300:
            say = (f"{m.sender} का ईमेल, {when}: “{m.subject}”। {_clip(body, 300)}" if hi else
                   f"{m.sender} wrote {when}, subject “{m.subject}”: {_clip(body, 300)}")
            return Result(True, _sentence(say, hi), data)
        assistant, owner = self.names()
        text = self._write(
            f"You are {assistant}, {owner}'s voice assistant. Tell {owner} what this email says, spoken aloud: at "
            "most 4 short sentences, no lists or markdown. Start with who wrote and what they want; mention dates, "
            f"amounts or questions that need an answer. {_lang(hi)}",
            f"From: {m.sender} <{m.sender_addr}>\nDate: {m.date}\nSubject: {m.subject}\n\n{body}", 350)
        if text is None:
            text = (f"{m.sender} का ईमेल: {m.subject}।" if hi else
                    f"{m.sender} wrote {when}, subject “{m.subject}”: {_clip(body, 200)}")
        return Result(True, _sentence(text, hi), data)

    # -- drafts and sending ---------------------------------------------------------------
    def _recipient(self, to: str, hi: bool) -> tuple[str, str, Mail | None] | Result:
        """(name, address, the mail this answers) for a name, an address, or "him"."""
        to = " ".join(to.split())[:80]
        ctx = self._mail_ctx()
        if (not to or to.lower() in PRONOUNS) and ctx is not None:
            return ctx.sender, ctx.sender_addr, ctx
        if not to:
            return Result(False, "किसे लिखूँ, समझ नहीं आया।" if hi else "Who should the email go to?", {})
        if ctx is not None and (to.lower() == ctx.sender_addr or to.lower() in ctx.sender.lower()):
            return ctx.sender, ctx.sender_addr, ctx
        found = self.gmail.find_address(to)
        if found is None:
            return Result(False, f"{to} का ईमेल पता नहीं मिला।" if hi else
                          f"I couldn't find an email address for {to} in your mail. Say the address, or mail them once.",
                          {"to": to})
        return found[0], found[1], None

    def _compose(self, args: dict, hi: bool) -> Draft | Result:
        about = " ".join(str(args.get("about") or args.get("text") or "").split())[:1000]
        if not about:
            return Result(False, "ईमेल में क्या लिखूँ, समझ नहीं आया।" if hi else "What should the email say?", {})
        who = self._recipient(str(args.get("to") or ""), hi)
        if isinstance(who, Result):
            return who
        name, addr, reply = who
        if self.writer is None:
            return Result(False, "ईमेल लिखने के लिए AI चाहिए।" if hi else "I need the AI to write an email.", {})
        assistant, owner = self.names()
        context = ""
        if reply is not None:
            context = (f"\n\nThis replies to their email (subject: {reply.subject}):\n"
                       f"{_clip(reply.body or reply.snippet, 1500)}")
        try:
            out = self.writer.write_json(
                f"Write a short email from {owner} to {name}, in {owner}'s own voice (first person), friendly and "
                f"natural, no placeholders like [Name]. Sign it with {owner.split(' ')[0] if owner else 'me'}. "
                f"{_lang(hi) if hi else 'Write in English unless the request is in another language.'} "
                'Return JSON: {"subject": "...", "body": "..."} (body as plain text with line breaks).',
                f"What the email should say: {about}{context}", 600)
        except (LLMUnavailable, ValueError) as exc:
            log.warning("draft not written: %s", exc)
            return Result(False, "अभी ईमेल नहीं लिख पाई।" if hi else f"I couldn't write the email right now: {exc}.", {})
        body = str(out.get("body") or "").strip()[:5000]
        subject = " ".join(str(out.get("subject") or "").split())[:150]
        if reply is not None and reply.subject and not subject.lower().startswith("re:"):
            subject = f"Re: {reply.subject}"[:150]
        if not body:
            return Result(False, "ईमेल नहीं लिख पाई।" if hi else "I couldn't write that email.", {})
        did = self.gmail.create_draft(f"{name} <{addr}>" if name != addr else addr, subject or "(no subject)", body, reply)
        d = Draft(did, name, addr, subject or "(no subject)", body, reply, time.monotonic())
        self.last_draft = d
        return d

    def email_draft(self, args: dict, hi: bool) -> Result:
        d = self._compose(args, hi)
        if isinstance(d, Result):
            return d
        say = (f"{d.to_name} के लिए ड्राफ़्ट Gmail में सेव कर दिया है, विषय “{d.subject}”। भेजना हो तो “भेज दो” कहिए।" if hi else
               f"I saved a draft to {d.to_name} in Gmail, subject “{d.subject}”. It's on screen; say “send it” to send it.")
        return Result(True, say, {"draft": d.text(), "to": d.to_addr})

    def plan_send(self, args: dict, hi: bool) -> Draft | Result:
        """The draft to send (written now, or the latest one), for the read-back and confirmation."""
        if args.get("about") or args.get("text"):
            return self._compose(args, hi)
        d = self.last_draft
        if d is None or time.monotonic() - d.t > CONTEXT_TTL_S:
            return Result(False, "भेजने के लिए कोई ड्राफ़्ट नहीं है। बताइए क्या लिखूँ।" if hi
                          else "There's no draft to send. Tell me who it's for and what to say.", {})
        to = " ".join(str(args.get("to") or "").split()).lower()
        if to and to not in PRONOUNS and to not in d.to_name.lower() and to != d.to_addr:
            return Result(False, f"आख़िरी ड्राफ़्ट {d.to_name} के लिए है।" if hi else
                          f"The latest draft is to {d.to_name}, not {to}. Tell me what to write to {to}.", {})
        return d

    def send(self, d: Draft, hi: bool) -> Result:
        try:
            self.gmail.send_draft(d.id)
        except GoogleError as exc:
            return Result(False, _sentence(f"I couldn't send it: {exc}", hi), {})
        if self.last_draft is d:
            self.last_draft = None
        return Result(True, f"{d.to_name} को ईमेल भेज दिया है।" if hi else f"Sent your email to {d.to_name}.",
                      {"to": d.to_addr, "subject": d.subject})

    # -- Drive -------------------------------------------------------------------------------
    def _names(self, files: list[DriveFile], hi: bool) -> str:
        names = [f"“{_clip(f.name, 60)}”" for f in files[:3]]
        return ", ".join(names[:-1]) + (" और " if hi else " and ") + names[-1] if len(names) > 1 else names[0]

    def drive_search(self, args: dict, hi: bool) -> Result:
        q = " ".join(str(args.get("query") or "").split())[:100]
        if not q:
            return Result(False, "Drive में क्या ढूँढूँ?" if hi else "What should I look for in your Drive?", {})
        files = self.drive.search(q)
        self.last_files = files
        data = {"files": [f.public() for f in files]}
        if not files:
            return Result(True, f"Drive में “{q}” से जुड़ा कुछ नहीं मिला।" if hi else f"Nothing in your Drive matches “{q}”.", data)
        say = (f"Drive में {len(files)} फ़ाइलें मिलीं: {self._names(files, True)}।" if hi else
               f"I found {len(files)} in your Drive: {self._names(files, False)}. Say “open it” to open the first.")
        return Result(True, say, data)

    def drive_recent(self, args: dict, hi: bool) -> Result:
        files = self.drive.recent()
        self.last_files = files
        data = {"files": [f.public() for f in files]}
        if not files:
            return Result(True, "Drive खाली है।" if hi else "Your Drive has no files.", data)
        f = files[0]
        return Result(True, (f"हाल की फ़ाइलें: {self._names(files, True)}।" if hi else
                             f"Your latest Drive files are {self._names(files, False)}; "
                             f"“{_clip(f.name, 60)}” changed {self._when(f.modified, False)}."), data)

    def _one(self, args: dict, hi: bool) -> DriveFile | Result:
        q = " ".join(str(args.get("query") or "").split())[:100]
        if q:
            files = self.drive.search(q, 3)
            self.last_files = files
            if not files:
                return Result(False, f"Drive में “{q}” नहीं मिला।" if hi else f"I couldn't find “{q}” in your Drive.", {})
            return files[0]
        if self.last_files:
            return self.last_files[0]
        return Result(False, "कौन सी फ़ाइल?" if hi else "Which file? Tell me its name.", {})

    def drive_open(self, args: dict, hi: bool) -> Result:
        f = self._one(args, hi)
        if isinstance(f, Result):
            return f
        if not f.link.startswith("https://"):
            return Result(False, "यह फ़ाइल खोल नहीं सकती।" if hi else "I can't open that file.", {})
        self.open_url(f.link)
        return Result(True, f"“{f.name}” खोल रही हूँ।" if hi else f"Opening “{_clip(f.name, 60)}” in your browser.",
                      {"files": [f.public()]})

    def drive_summarize(self, args: dict, hi: bool) -> Result:
        f = self._one(args, hi)
        if isinstance(f, Result):
            return f
        data = {"files": [f.public()]}
        if not f.readable:
            return Result(False, (f"“{f.name}” ({f.kind}) मैं पढ़ नहीं सकती, पर खोल सकती हूँ।" if hi else
                                  f"I can't read “{_clip(f.name, 60)}” (a {f.kind}); say “open it” and I'll open it."), data)
        text = self.drive.text(f) or ""
        if not text.strip():
            return Result(True, f"“{f.name}” खाली है।" if hi else f"“{_clip(f.name, 60)}” is empty.", data)
        assistant, owner = self.names()
        out = self._write(
            f"You are {assistant}, {owner}'s voice assistant. Summarise this document for {owner}, spoken aloud: at "
            f"most 5 short sentences, no lists or markdown. {_lang(hi)}", f"Document: {f.name}\n\n{text}", 400)
        if out is None:
            return Result(False, "अभी सारांश नहीं बना पाई।" if hi else "I can't summarise right now (the AI isn't available).", data)
        return Result(True, _sentence(out, hi), data)
