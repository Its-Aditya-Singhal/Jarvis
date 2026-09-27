"""Brain → tools → reply inside AssistantService (auth levels checked at execution time)."""

from datetime import datetime, timedelta

from jarvis.auth.levels import Trust
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


def fake_trust(state):
    level = state["level"]
    blockers = {} if level >= 2 else {2: state.get("why", "voice_needed"), 3: state.get("why", "voice_needed")}
    if level == 0:
        blockers[1] = "absent"
    if level >= 2 and not state.get("l3", True):
        blockers[3] = "fresh_liveness"
    return Trust(level, 0.99, blockers=blockers, l3_ready=level >= 2 and state.get("l3", True),
                 needs_fresh_liveness=level >= 2 and not state.get("l3", True))


def make(settings, actions, level=2):
    db = Database(settings.db_path)
    store = TemplateStore(settings.templates_dir, StaticKeyProvider())
    tstore = ToolStore(db, StaticKeyProvider())

    def tools(svc):
        runner = ToolRunner(db, tstore, AppIndex([settings.data_dir]), FileSearch(db), None, on_change=svc.tools_changed)
        return runner, AlarmScheduler(tstore, on_ring=svc.ring)

    svc = AssistantService(settings, db, store, EventBus(), Nothing(), Nothing(),
                           brain_factory=lambda s: FakeBrain(actions), tools_factory=tools)
    state = {"level": level}
    svc.trust = lambda now=None: fake_trust(state)
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
        state["level"] = 0  # owner walks away after the first tool
        return real(action, lang)

    svc.tools.run = run_then_leave
    out = svc.command("two notes")
    assert calls["n"] == 1 and [n.text for n in tstore.notes()] == ["one"]
    assert "no longer verified" in out["reply"]
    assert db.security_events(5)[0]["kind"] == "tool_blocked"


def test_level1_can_read_but_not_create(settings):
    svc, tstore, db, state = make(settings, [Action("notes.search", {"query": "milk"}),
                                             Action("notes.add", {"text": "Buy milk"})], level=1)
    out = svc.command("find milk and add a note", source="typed")
    assert [a["ok"] for a in out["actions"]] == [True, False]
    assert out["actions"][1]["data"]["blocked"] == "voice_needed"
    assert "hear your voice" in out["reply"] and tstore.notes() == []
    ev = db.security_events(5)[0]
    assert ev["kind"] == "tool_blocked" and ev["blocked"] and "needs level 2" in ev["detail"]


def test_voice_command_with_unclear_voice_asks_to_repeat(settings):
    svc, tstore, _, _ = make(settings, [Action("notes.add", {"text": "x"})], level=1)
    out = svc.command("note x", source="voice")
    assert "couldn't confirm your voice" in out["reply"]


def test_bystander_caps_at_read(settings):
    svc, tstore, _, state = make(settings, [Action("app.open", {"name": "Notes"})], level=1)
    state["why"] = "bystander"
    out = svc.command("open notes")
    assert "Someone else is in view" in out["reply"] and not out["actions"][0]["ok"]


def _with_note(settings, text="Call the bank on Monday", **kw):
    svc, tstore, db, state = make(settings, [Action("notes.delete", {"query": "bank"})], **kw)
    tstore.add_note(text)
    tstore.add_note("Buy milk")
    return svc, tstore, db, state


def test_delete_waits_for_confirmation_then_deletes(settings):
    svc, tstore, db, _ = _with_note(settings)
    events = []
    svc.bus.on("confirm", events.append)
    out = svc.command("delete the bank note")
    assert "Delete the note “Call the bank on Monday”?" in out["reply"]
    assert len(tstore.notes()) == 2  # nothing deleted yet
    pid = out["actions"][0]["data"]["pending"]
    assert events and events[0]["id"] == pid and svc.pending() is not None

    assert not svc.confirm("wrong-id", True, "click")["ok"]
    res = svc.confirm(pid, True, "click")
    assert res["ok"] and res["reply"].startswith("Deleted the note")
    assert [n.text for n in tstore.notes()] == ["Buy milk"]
    assert svc.pending() is None
    assert db.security_events(5)[0]["kind"] == "sensitive_action"
    assert not svc.confirm(pid, True, "click")["ok"]  # single use


def test_delete_can_be_declined(settings):
    svc, tstore, _, _ = _with_note(settings)
    pid = svc.command("delete the bank note")["actions"][0]["data"]["pending"]
    assert "won't delete" in svc.confirm(pid, False, "click")["reply"]
    assert len(tstore.notes()) == 2 and svc.pending() is None


def test_spoken_confirmation_needs_a_verified_voice(settings):
    svc, tstore, _, _ = _with_note(settings)
    pid = svc.command("delete the bank note")["actions"][0]["data"]["pending"]
    assert "couldn't verify your voice" in svc.confirm(pid, True, "voice", verdict=None)["reply"]
    assert "couldn't verify your voice" in svc.confirm(pid, True, "voice", verdict="uncertain")["reply"]
    assert len(tstore.notes()) == 2 and svc.pending() is not None  # still waiting
    svc.confirm_spoken(True, "verified")
    assert len(tstore.notes()) == 1


def test_confirmation_rechecks_the_level(settings):
    svc, tstore, db, state = _with_note(settings)
    pid = svc.command("delete the bank note")["actions"][0]["data"]["pending"]
    state["level"] = 1  # e.g. a stranger's voice was heard in between
    state["why"] = "voice_rejected"
    assert not svc.confirm(pid, True, "click")["ok"]
    assert len(tstore.notes()) == 2
    assert db.security_events(5)[0]["kind"] == "tool_blocked"


def test_stale_liveness_triggers_a_challenge_before_deleting(settings):
    svc, tstore, _, state = _with_note(settings)
    state["l3"] = False
    out = svc.command("delete the bank note")
    assert out["reply"].startswith("First a quick liveness check.")
    assert svc._demand_liveness
    pid = out["actions"][0]["data"]["pending"]
    assert "Finish the liveness check" in svc.confirm(pid, True, "click")["reply"]
    state["l3"] = True  # challenge passed
    assert svc.confirm(pid, True, "click")["ok"] and len(tstore.notes()) == 1


def test_pending_deletion_expires(settings):
    svc, tstore, _, _ = _with_note(settings)
    pid = svc.command("delete the bank note")["actions"][0]["data"]["pending"]
    svc._pending.expires = 0.0
    assert svc.pending() is None and not svc.confirm(pid, True, "click")["ok"]
    assert len(tstore.notes()) == 2


def test_delete_without_match_reports_honestly(settings):
    svc, tstore, _, _ = make(settings, [Action("notes.delete", {"query": "passport"})])
    tstore.add_note("Buy milk")
    out = svc.command("delete passport note")
    assert out["reply"] == "No note matches “passport”." and svc.pending() is None


def test_level3_tools_never_run_directly(settings):
    svc, tstore, _, _ = _with_note(settings)
    res = svc.tools.run(Action("notes.delete", {"query": "bank"}), "en")
    assert not res.ok and len(tstore.notes()) == 2


def test_owner_commands_feed_fusion_samples(settings):
    svc, _, _, _ = make(settings, [Action("notes.add", {"text": "a"})])
    svc.mode = "verifying"  # samples are only kept while verifying
    svc.command("note a")
    assert svc.samples.counts() == {"owner": 1, "other": 0}
