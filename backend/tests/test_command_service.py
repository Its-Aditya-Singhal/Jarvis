"""Brain → tools → reply inside AssistantService (owner checks at execution time)."""

from datetime import datetime, timedelta

import numpy as np

from jarvis.brain import BrainResult
from jarvis.database.db import Database
from jarvis.events import EventBus
from jarvis.llm.intents import Action
from jarvis.security.crypto import StaticKeyProvider
from jarvis.security.template_store import TemplateStore
from jarvis.service import AssistantService
from jarvis.tools.apps import AppIndex
from jarvis.tools.files import FileSearch
from jarvis.tools.runner import ToolRunner
from jarvis.tools.scheduler import AlarmScheduler
from jarvis.tools.store import ToolStore


class FakeBrain:
    model = "fake"

    def __init__(self, actions, chat=""):
        self.actions, self.chat = actions, chat

    def start(self): return True
    def stop(self): ...
    def clear(self): ...
    def status(self): return "ready"

    def respond(self, text, lang="en"):
        return BrainResult("planned (unused)", lang, self.actions, 0.1, True, self.chat)


class Nothing:
    ready, error, status = True, None, "active"
    def load(self): return True
    def start(self): ...
    def stop(self): ...
    def latest(self, max_age_s=1.0): return None
    def analyze(self, frame): return []


def make(settings, actions, verified=True):
    db = Database(settings.db_path)
    store = TemplateStore(settings.templates_dir, StaticKeyProvider())
    tstore = ToolStore(db, StaticKeyProvider())

    def tools(svc):
        runner = ToolRunner(db, tstore, AppIndex([settings.data_dir]), FileSearch(db), None, on_change=svc.tools_changed)
        return runner, AlarmScheduler(tstore, on_ring=svc.ring)

    svc = AssistantService(settings, db, store, EventBus(), Nothing(), Nothing(),
                           brain_factory=lambda s: FakeBrain(actions), tools_factory=tools)
    state = {"verified": verified}
    svc.owner_verified = lambda: state["verified"]
    return svc, tstore, db, state


def test_actions_run_and_reply_reports_real_results(settings):
    t = (datetime.now() + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    svc, tstore, db, _ = make(settings, [
        Action("alarm.set", {"time": t.isoformat(timespec="minutes")}),
        Action("notes.add", {"text": "Buy milk"}),
        Action("app.open", {"name": "Nonexistent App"}),
    ])
    out = svc.command("wake me at 7, note milk, open nonexistent app")
    assert out["reply"].startswith("Alarm set for 7:00 AM tomorrow. Note saved. I couldn't find an app")
    assert [a["ok"] for a in out["actions"]] == [True, True, False]
    assert len(tstore.alarms()) == 1 and tstore.notes()[0].text == "Buy milk"


def test_owner_leaving_mid_command_stops_the_rest(settings):
    svc, tstore, db, state = make(settings, [Action("notes.add", {"text": "one"}), Action("notes.add", {"text": "two"})])
    calls = {"n": 0}
    real = svc.tools.run

    def run_then_leave(action, lang):
        calls["n"] += 1
        state["verified"] = False  # owner walks away after the first tool
        return real(action, lang)

    svc.tools.run = run_then_leave
    out = svc.command("two notes")
    assert calls["n"] == 1 and [n.text for n in tstore.notes()] == ["one"]
    assert "no longer verified" in out["reply"]
    assert db.security_events(5)[0]["kind"] == "tool_blocked"
