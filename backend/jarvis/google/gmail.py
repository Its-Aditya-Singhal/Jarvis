"""Gmail over its REST API: list, read, draft and send (drafts are sent only after a confirmation)."""

from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any

from .auth import GoogleAuth, GoogleError

API = "https://gmail.googleapis.com/gmail/v1/users/me"
HEADERS = ("From", "To", "Cc", "Subject", "Date", "Message-ID")
MAX_BODY = 8000
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


@dataclass
class Mail:
    id: str
    thread_id: str
    sender: str  # display name (or the address when there is none)
    sender_addr: str
    subject: str
    date: datetime | None
    snippet: str
    unread: bool = False
    message_id: str = ""  # the RFC 822 Message-ID, for replies
    to: list[tuple[str, str]] = field(default_factory=list)
    body: str = ""

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "from": self.sender, "address": self.sender_addr, "subject": self.subject,
                "date": self.date.isoformat(timespec="minutes") if self.date else None,
                "snippet": self.snippet, "unread": self.unread}


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def html_text(markup: str) -> str:
    """Readable text from an HTML mail (no parser dependency: mails are only summarised)."""
    t = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", markup)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    return re.sub(r"[ \t ]+", " ", re.sub(r"\n\s*\n+", "\n\n", t)).strip()


def body_text(payload: dict) -> str:
    """The text/plain part of a message (the HTML part as text when there is none)."""
    plain: list[str] = []
    rich: list[str] = []

    def walk(part: dict) -> None:
        mime = str(part.get("mimeType") or "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            rich.append(_b64(data))
        for p in part.get("parts") or []:
            walk(p)

    walk(payload)
    text = "\n".join(plain) if plain else html_text("\n".join(rich))
    # quoted earlier messages in a reply chain are noise for a summary
    text = re.split(r"\n(?:On .{5,120} wrote:|-{2,} ?Original Message ?-{2,})", text, maxsplit=1)[0]
    return text.strip()[:MAX_BODY]


def _when(value: str) -> datetime | None:
    try:
        d = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


def to_mail(msg: dict) -> Mail:
    payload = msg.get("payload") or {}
    h = {str(x.get("name") or "").lower(): str(x.get("value") or "") for x in payload.get("headers") or []}
    name, addr = parseaddr(h.get("from", ""))
    m = Mail(
        id=str(msg.get("id") or ""), thread_id=str(msg.get("threadId") or ""),
        sender=(name or addr or "someone").strip('"'), sender_addr=addr.lower(),
        subject=" ".join(h.get("subject", "").split()) or "(no subject)", date=_when(h.get("date", "")),
        snippet=html.unescape(str(msg.get("snippet") or "")), unread="UNREAD" in (msg.get("labelIds") or []),
        message_id=h.get("message-id", ""), to=getaddresses([h.get("to", ""), h.get("cc", "")]),
    )
    if payload.get("parts") or (payload.get("body") or {}).get("data"):
        m.body = body_text(payload)
    return m


class Gmail:
    def __init__(self, auth: GoogleAuth):
        self.auth = auth

    def _get(self, path: str, **params: Any) -> dict:
        return self.auth.call("GET", f"{API}/{path}", "gmail", params=params).json()

    def ids(self, query: str, n: int) -> tuple[list[str], bool]:
        """Newest-first message ids matching a Gmail search, and whether there are more."""
        data = self._get("messages", q=query, maxResults=max(1, min(n, 100)))
        return [str(m["id"]) for m in data.get("messages") or [] if m.get("id")], bool(data.get("nextPageToken"))

    def message(self, mid: str, full: bool = False) -> Mail:
        if full:
            return to_mail(self._get(f"messages/{mid}", format="full"))
        return to_mail(self._get(f"messages/{mid}", format="metadata", metadataHeaders=list(HEADERS)))

    def search(self, query: str, n: int = 10) -> list[Mail]:
        return [self.message(i) for i in self.ids(query, n)[0]]

    def unread(self, n: int = 10) -> tuple[int, bool, list[Mail]]:
        ids, more = self.ids("is:unread in:inbox", 100)
        return len(ids), more, [self.message(i) for i in ids[:n]]

    def find_address(self, name: str) -> tuple[str, str] | None:
        """(display name, address) of someone the owner has mailed with, by (part of) their name."""
        name = " ".join(name.split()).strip()
        if EMAIL_RE.match(name):  # an address: just its display name from past mail, if any
            addr = name.lower()
            for mid in self.ids(f"from:{addr} OR to:{addr}", 3)[0]:
                msg = self._get(f"messages/{mid}", format="metadata", metadataHeaders=["From", "To", "Cc"])
                heads = [str(h.get("value") or "") for h in (msg.get("payload") or {}).get("headers") or []]
                for disp, a in getaddresses(heads):
                    if a.lower() == addr and disp.strip('"'):
                        return disp.strip('"'), addr
            return addr, addr
        words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 1]
        if not words:
            return None
        q = " OR ".join(f"from:{w} OR to:{w}" for w in words[:2])
        best: tuple[int, str, str] | None = None
        for mid in self.ids(q, 10)[0]:
            msg = self._get(f"messages/{mid}", format="metadata", metadataHeaders=["From", "To", "Cc"])
            heads = [str(h.get("value") or "") for h in (msg.get("payload") or {}).get("headers") or []]
            for disp, addr in getaddresses(heads):
                hay = f"{disp} {addr.split('@')[0]}".lower()
                score = sum(w in hay for w in words)
                if score and addr and (best is None or score > best[0]):
                    best = (score, disp.strip('"') or addr, addr.lower())
            if best and best[0] == len(words):
                break
        return (best[1], best[2]) if best else None

    @staticmethod
    def raw(to: str, subject: str, body: str, reply: Mail | None = None) -> str:
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        if reply is not None and reply.message_id:
            msg["In-Reply-To"] = reply.message_id
            msg["References"] = reply.message_id
        msg.set_content(body)
        return base64.urlsafe_b64encode(msg.as_bytes()).decode()

    def create_draft(self, to: str, subject: str, body: str, reply: Mail | None = None) -> str:
        message: dict[str, Any] = {"raw": self.raw(to, subject, body, reply)}
        if reply is not None and reply.thread_id:
            message["threadId"] = reply.thread_id
        r = self.auth.call("POST", f"{API}/drafts", "gmail", json={"message": message}).json()
        did = str(r.get("id") or "")
        if not did:
            raise GoogleError("Gmail didn't save the draft")
        return did

    def send_draft(self, draft_id: str) -> str:
        r = self.auth.call("POST", f"{API}/drafts/send", "gmail", json={"id": draft_id}).json()
        return str(r.get("id") or "")
