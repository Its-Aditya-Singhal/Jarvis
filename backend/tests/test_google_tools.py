"""Gmail, Drive and Google Calendar tools against a scripted Google API (no network)."""

import base64
import json
from datetime import datetime
from email import message_from_bytes
from email.utils import getaddresses
from urllib.parse import parse_qs

import httpx
import pytest
from test_google import CID, SECRET, Google, connect
from test_tools import env  # noqa: F401 (a fixture)

from jarvis.database.db import Database
from jarvis.google.auth import GoogleAuth
from jarvis.llm.client import LLMUnavailable
from jarvis.security.crypto import StaticKeyProvider
from jarvis.security.secrets import Secrets
from jarvis.tools.google import Draft, GoogleTools

NOW = datetime(2026, 10, 1, 10, 0)


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def msg(mid: str, frm: str, subject: str, snippet: str, body: str = "", unread=False, date="Wed, 30 Sep 2026 18:00:00 +0530"):
    return {"id": mid, "threadId": "t" + mid, "snippet": snippet, "labelIds": ["INBOX"] + (["UNREAD"] if unread else []),
            "payload": {"mimeType": "multipart/alternative", "headers": [
                {"name": "From", "value": frm}, {"name": "To", "value": "Aditya <aditya@gmail.com>"},
                {"name": "Subject", "value": subject}, {"name": "Date", "value": date},
                {"name": "Message-ID", "value": f"<{mid}@mail.example>"}],
                "parts": [{"mimeType": "text/plain", "body": {"data": b64(body)}}] if body else []}}


class Mailbox:
    """A tiny Gmail: messages, a search on from: and is:unread, drafts and sending."""

    def __init__(self):
        self.messages = [
            msg("m1", "Rahul Sharma <rahul@example.com>", "Dinner on Friday?", "Are you coming on Friday at 8?",
                "Hey Aditya,\n\nAre you coming to dinner on Friday at 8? Let me know by tomorrow.\n\nOn Tue someone wrote:\n> old",
                unread=True),
            msg("m2", "Amazon <ship@amazon.in>", "Your order has shipped", "Arriving Thursday", unread=True),
            msg("m3", "LinkedIn <news@linkedin.com>", "5 new jobs", "Jobs for you"),
        ]
        self.drafts: dict[str, dict] = {}
        self.sent: list[str] = []
        self.queries: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/gmail/v1/users/me/")
        q = parse_qs(request.url.query.decode())
        if path == "messages":
            query = q.get("q", [""])[0]
            self.queries.append(query)
            found = self.messages
            if "is:unread" in query:
                found = [m for m in found if "UNREAD" in m["labelIds"]]
            for part in query.split():
                if part.startswith("from:"):
                    who = part[5:].strip('"').lower()
                    found = [m for m in found if who in json.dumps(m["payload"]["headers"]).lower()]
            n = int(q.get("maxResults", ["10"])[0])
            return httpx.Response(200, json={"messages": [{"id": m["id"]} for m in found[:n]], "resultSizeEstimate": len(found)})
        if path.startswith("messages/"):
            m = next(m for m in self.messages if m["id"] == path.split("/")[1])
            if q.get("format") == ["full"]:
                return httpx.Response(200, json=m)
            return httpx.Response(200, json={**m, "payload": {**m["payload"], "parts": []}})
        if path == "drafts":
            raw = json.loads(request.content)["message"]
            did = f"d{len(self.drafts) + 1}"
            self.drafts[did] = raw
            return httpx.Response(200, json={"id": did})
        if path == "drafts/send":
            did = json.loads(request.content)["id"]
            self.sent.append(did)
            return httpx.Response(200, json={"id": "sent" + did})
        return httpx.Response(404)


class Writer:
    def __init__(self, fail=False):
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def write(self, system, text, max_tokens=700):
        self.calls.append((system, text))
        if self.fail:
            raise LLMUnavailable("quota")
        return "Rahul asks if you're coming to dinner on Friday at 8."

    def write_json(self, system, text, max_tokens=700):
        self.calls.append((system, text))
        if self.fail:
            raise LLMUnavailable("quota")
        return {"subject": "Friday", "body": "Hi Rahul,\n\nYes, I'll be there at 8.\n\nAditya"}


@pytest.fixture
def tools(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    fake = Google()
    box = Mailbox()
    fake.api["https://gmail.googleapis.com/"] = [box] * 100
    auth = GoogleAuth(db, Secrets(db, StaticKeyProvider()), transport=httpx.MockTransport(fake))
    connect(auth)
    opened: list[str] = []
    t = GoogleTools(auth, names=lambda: ("JARVIS", "Aditya"), open_url=opened.append, clock=lambda: NOW)
    t.writer = Writer()
    t.box, t.fake, t.opened = box, fake, opened  # type: ignore[attr-defined]
    yield t
    db.close()


def test_unread_counts_and_names_senders_without_ai(tools):
    r = tools.run("email.unread", {}, False)
    assert r.ok and r.say == "You have 2 unread emails, from Rahul Sharma and Amazon."
    assert tools.writer.calls == []  # no AI request for a count


def test_summary_is_one_writing_request_over_headers_and_snippets(tools):
    r = tools.run("email.summary", {"count": 10}, False)
    assert r.ok and "dinner" in r.say and len(r.data["mails"]) == 3
    assert len(tools.writer.calls) == 1
    system, text = tools.writer.calls[0]
    assert "Rahul Sharma" in text and "Dinner on Friday?" in text and "Aditya" in system
    assert "Let me know by tomorrow" not in text  # only snippets, not bodies


def test_summary_without_ai_still_answers(tools):
    tools.writer = Writer(fail=True)
    r = tools.run("email.summary", {}, False)
    assert r.ok and "Rahul Sharma" in r.say and "can't summarise" in r.say


def test_read_one_mail_by_sender_drops_the_quoted_chain(tools):
    r = tools.run("email.read", {"from": "Rahul"}, False)
    assert r.ok and "from:Rahul" in tools.box.queries[-1]
    # a short mail is read out as it is (no AI request), without the quoted earlier messages
    assert "Let me know by tomorrow" in r.say and "> old" not in r.say and tools.writer.calls == []
    assert tools.last_mail[0].sender_addr == "rahul@example.com"


def test_draft_to_him_after_reading_replies_in_the_thread(tools):
    tools.run("email.read", {"from": "Rahul"}, False)
    r = tools.run("email.draft", {"to": "him", "about": "confirm I'll come"}, False)
    assert r.ok and "Rahul Sharma" in r.say and "send it" in r.say
    raw = tools.box.drafts["d1"]
    assert raw["threadId"] == "tm1"
    mail = message_from_bytes(base64.urlsafe_b64decode(raw["raw"]))
    assert "rahul@example.com" in mail["To"] and mail["Subject"] == "Re: Dinner on Friday?"
    assert mail["In-Reply-To"] == "<m1@mail.example>"
    assert tools.box.sent == []  # a draft is never sent


def test_a_name_is_resolved_from_past_mail(tools):
    r = tools.run("email.draft", {"to": "Rahul", "about": "lunch"}, False)
    assert r.ok and r.data["to"] == "rahul@example.com"
    r = tools.run("email.draft", {"to": "Nobody Known", "about": "hi"}, False)
    assert not r.ok and "couldn't find an email address" in r.say


def test_a_display_name_cant_add_a_recipient(tools):
    """A name with a comma and an address in it (set by whoever sent the mail) stays one quoted name."""
    tools.box.messages.append(msg("m4", '"Priya, mallory@evil.test" <priya@example.com>', "Hi", "Hello"))
    r = tools.run("email.draft", {"to": "Priya", "about": "lunch"}, False)
    assert r.ok and r.data["to"] == "priya@example.com"
    mail = message_from_bytes(base64.urlsafe_b64decode(tools.box.drafts["d1"]["raw"]))
    assert [a for _, a in getaddresses([mail["To"]])] == ["priya@example.com"]


def test_send_plans_the_latest_draft_and_sends_only_on_execute(tools):
    nothing = tools.plan_send({}, False)
    assert not isinstance(nothing, Draft) and "no draft" in nothing.say
    tools.run("email.draft", {"to": "rahul@example.com", "about": "yes"}, False)
    d = tools.plan_send({"to": "him"}, False)
    assert isinstance(d, Draft) and tools.box.sent == []
    other = tools.plan_send({"to": "Priya"}, False)
    assert not isinstance(other, Draft) and "latest draft is to" in other.say
    r = tools.send(d, False)
    assert r.ok and tools.box.sent == ["d1"] and tools.last_draft is None


def test_not_connected_is_said_plainly(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    auth = GoogleAuth(db, Secrets(db, StaticKeyProvider()), transport=httpx.MockTransport(Google()))
    r = GoogleTools(auth).run("email.unread", {}, False)
    assert not r.ok and "Settings" in r.say
    db.close()


def test_drive_search_summarize_and_open(tools):
    files = {"files": [{"id": "f1", "name": "Project plan", "mimeType": "application/vnd.google-apps.document",
                        "modifiedTime": "2026-09-30T10:00:00Z", "webViewLink": "https://docs.google.com/document/d/f1"}]}
    tools.fake.api["https://www.googleapis.com/drive/v3/files/f1/export"] = [httpx.Response(200, text="Phase one: design.")]
    tools.fake.api["https://www.googleapis.com/drive/v3/files"] = [httpx.Response(200, json=files)] * 5
    r = tools.run("drive.search", {"query": "project plan"}, False)
    assert r.ok and "Project plan" in r.say
    q = [str(c.url) for c in tools.fake.calls if "drive/v3/files?" in str(c.url)][0]
    assert "name+contains" in q and "trashed" in q
    r = tools.run("drive.summarize", {}, False)  # the file just found
    assert r.ok and "Phase one: design." in tools.writer.calls[-1][1]
    r = tools.run("drive.open", {}, False)
    assert r.ok and tools.opened == ["https://docs.google.com/document/d/f1"]


def test_runner_and_service_read_the_mail_back_before_sending(tmp_path):
    """The whole path: email.send is level 3, its question reads the draft out, the click sends it."""
    import time

    from test_api import H, _client, _voice_ready
    from test_voice_only import _enroll_voice, _voice_only

    from jarvis.llm.intents import Action

    s = _voice_only(tmp_path)
    db = Database(s.db_path)
    fake = Google()
    box = Mailbox()
    fake.api["https://gmail.googleapis.com/"] = [box] * 100
    keys = StaticKeyProvider()
    auth = GoogleAuth(db, Secrets(db, keys), transport=httpx.MockTransport(fake))
    client, svc = _client(s, keys=keys, google=auth, tools=True)
    with client:
        _voice_ready(svc)
        client.post("/api/setup/profile", headers=H, json={"owner_name": "Aditya", "assistant_name": "JARVIS"})
        _enroll_voice(svc)
        client.post("/api/setup/complete", headers=H)
        auth.set_client(CID, SECRET)
        connect(auth)
        svc.tools.google.writer = Writer()
        said = []
        svc.bus.on("say", lambda e: said.append(e["text"]))
        svc.voice.auth.judge(0.9, 0.9, time.monotonic(), 2.0)
        results = svc._run_tools([Action("email.send", {"to": "rahul@example.com", "about": "yes I'll come"}, "")], "en", "voice")
        assert results[0].ok and "Here's the email to Rahul" in results[0].say and "I'll be there at 8" in results[0].say
        assert "(rahul@example.com)" in results[0].say  # the address is read out, not only the name
        p = svc.pending()
        assert p is not None and p.plan.tool == "email.send" and "Subject: Friday" in p.plan.detail
        assert p.expires - svc.clock() > s.confirm_ttl_s  # time to hear it read out
        assert box.sent == []
        r = svc.confirm(p.id, True, "voice", verdict="uncertain")
        assert not r["ok"] and box.sent == []  # a doubtful "yes" doesn't send
        r = client.post(f"/api/confirm/{p.id}", headers=H, json={"accept": True})
        assert r.status_code == 200 and box.sent == ["d1"]
    db.close()


def test_google_calendar_is_listed_and_written_alongside_the_local_one(env, tools):  # noqa: F811
    from test_tools import run

    runner, store, *_ = env
    runner.google = tools
    events = {"items": [
        {"id": "g1", "summary": "Dentist", "start": {"dateTime": "2026-09-28T09:30:00+00:00"},
         "end": {"dateTime": "2026-09-28T10:00:00+00:00"}},
        {"id": "g2", "summary": "Holiday", "start": {"date": "2026-09-28"}, "end": {"date": "2026-09-29"}},
        {"id": "g3", "summary": "Gone", "status": "cancelled", "start": {"date": "2026-09-28"}}]}
    url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
    created = []

    def create(request):
        created.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "new"})

    tools.fake.api[url] = [create, httpx.Response(200, json=events)]
    r = run(runner, "calendar.create", title="Team meeting", start="2026-09-28T17:00")
    assert r.ok and "also in Google Calendar" in r.say and r.data["google"]
    assert created[0]["summary"] == "Team meeting" and created[0]["start"]["dateTime"].startswith("2026-09-28T17:00:00")
    r = run(runner, "calendar.list", date="2026-09-28")
    titles = [e["title"] for e in r.data["events"]]
    assert set(titles) == {"Team meeting", "Dentist", "Holiday"} and "Gone" not in titles
    assert {e["source"] for e in r.data["events"] if e["title"] == "Dentist"} == {"google"}
    q = [str(c.url) for c in tools.fake.calls if str(c.url).startswith(url + "?")][0]
    assert "singleEvents=true" in q and "timeMin=2026-09-28T00" in q
