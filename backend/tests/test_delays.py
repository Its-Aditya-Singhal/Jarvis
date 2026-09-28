"""Delays ("open WhatsApp and close it after 10 seconds"), "it" across commands, the owner's
name for the assistant, dropping actions nobody asked for, and the prompt fitting the context."""

import threading
from datetime import datetime

from test_command_service import make

from jarvis.brain import Brain
from jarvis.config import Settings
from jarvis.hardware import SMALL, STANDARD
from jarvis.llm.intents import Action, build_messages, parse_intent

NOW = datetime(2026, 9, 28, 11, 40)


class Now:
    """Stands in for threading.Timer: fires only when the test says so."""

    made: list["Now"] = []

    def __init__(self, secs, fn, args=()):
        self.secs, self.fn, self.args, self.cancelled, self.daemon = secs, fn, args, False, False
        Now.made.append(self)

    def start(self): ...
    def cancel(self): self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.fn(*self.args)


def delayed_service(settings, monkeypatch, actions, level=2):
    Now.made = []
    monkeypatch.setattr("jarvis.service.threading.Timer", Now)
    svc, tstore, db, state = make(settings, actions, level=level)
    ran = []
    svc.tools.run = lambda a, lang: (ran.append(a.tool), __import__("jarvis.tools.runner", fromlist=["ToolResult"])
                                     .ToolResult(a.tool, True, f"{a.tool} done."))[1]
    return svc, state, ran


def acts(*specs):
    out = []
    for tool, args in specs:
        a = Action(tool, args)
        a.summary = {"app.open": "opening WhatsApp", "app.close": "closing WhatsApp", "wait": "waiting"}.get(tool, tool)
        out.append(a)
    return out


def test_actions_after_a_wait_run_later(settings, monkeypatch):
    svc, _, ran = delayed_service(settings, monkeypatch, acts(
        ("app.open", {"name": "WhatsApp"}), ("wait", {"seconds": 10}), ("app.close", {"name": "WhatsApp"})))
    out = svc.command("open whatsapp and close it after 10 seconds")
    assert ran == ["app.open"]
    assert out["reply"] == "app.open done. In 10 seconds: closing WhatsApp."
    assert out["actions"][2].get("ok") is None  # shown as still to come
    assert [d["summary"] for d in svc.delayed()] == ["closing WhatsApp"]
    replies = []
    real = svc.bus.publish
    svc.bus.publish = lambda e: (replies.append(e), real(e))
    [t] = Now.made
    assert t.secs == 10
    t.fire()
    assert ran == ["app.open", "app.close"] and svc.delayed() == []
    said = [e for e in replies if e["type"] == "reply"]
    assert said and said[0]["text"] == "app.close done." and said[0]["actions"][0]["ok"] is True


def test_a_delay_can_be_cancelled(settings, monkeypatch):
    svc, _, ran = delayed_service(settings, monkeypatch, acts(("wait", {"seconds": 60}), ("app.close", {"name": "Slack"})))
    svc.command("close slack in a minute")
    [d] = svc.delayed()
    assert svc.cancel_delayed(d["id"]) == 1
    Now.made[0].fire()
    assert ran == [] and svc.delayed() == []


def test_nobody_verified_when_it_fires_stops_it(settings, monkeypatch):
    svc, state, ran = delayed_service(settings, monkeypatch, acts(("wait", {"seconds": 5}), ("app.close", {"name": "Slack"})))
    svc.command("close slack in 5 seconds")
    state["level"] = 0  # the owner locked the Mac and left
    Now.made[0].fire()
    assert ran == []


def test_the_voice_that_asked_still_counts_when_it_fires(settings, monkeypatch):
    svc, state, ran = delayed_service(settings, monkeypatch, acts(("wait", {"seconds": 300}), ("app.close", {"name": "Slack"})))
    svc.command("close slack in 5 minutes")
    state["level"] = 1  # still here, but the voice window has passed
    Now.made[0].fire()
    assert ran == ["app.close"]


def test_a_delay_needs_the_level_of_what_it_delays(settings, monkeypatch):
    svc, state, ran = delayed_service(settings, monkeypatch, acts(("wait", {"seconds": 5}), ("app.close", {"name": "Slack"})),
                                      level=1)
    out = svc.command("close slack in 5 seconds")
    assert Now.made == [] and "couldn't confirm your voice" in out["reply"]


def test_too_long_or_empty_delays(settings, monkeypatch):
    svc, _, _ = delayed_service(settings, monkeypatch, acts(("wait", {"seconds": 9 * 3600}), ("app.close", {"name": "Slack"})))
    assert "set an alarm" in svc.command("close slack in 9 hours")["reply"] and Now.made == []


def test_stopping_the_service_cancels_delays(settings, monkeypatch):
    svc, _, ran = delayed_service(settings, monkeypatch, acts(("wait", {"seconds": 5}), ("app.close", {"name": "Slack"})))
    svc.command("close slack in 5 seconds")
    svc.cancel_delayed()
    assert Now.made[0].cancelled and svc.delayed() == []


def test_real_timer_fires(settings):
    """One real threading.Timer end to end (short)."""
    svc, tstore, db, state = make(settings, acts(("wait", {"seconds": 1}), ("notes.add", {"text": "later"})))
    done = threading.Event()
    real = svc.bus.publish
    svc.bus.publish = lambda e: (real(e), e["type"] == "reply" and "Note saved" in e["text"] and done.set())
    svc.command("note later in a second")
    assert done.wait(5) and tstore.notes()[0].text == "later"


# -- the brain -----------------------------------------------------------------------------------
class KV(dict):
    def get(self, k, default=None): return super().get(k, default)
    def set(self, k, v): self[k] = v


def brain(tmp_path):
    return Brain(Settings(data_dir=tmp_path), KV(), lambda: ("Friday", "Aditya"), lambda: "female",
                 server=object(), client=object(), clock=lambda: NOW)


def test_the_assistants_name_is_not_part_of_the_request(tmp_path):
    b = brain(tmp_path)
    b.is_app = lambda n: n.lower() == "safari"
    r = b.respond("Friday, open Safari")
    assert r.fast and [(a.tool, a.args) for a in r.actions] == [("app.open", {"name": "safari"})]
    assert b._unname("Hey Friday!") == "Hey Friday!"  # nothing but the name: leave it


def test_it_means_the_app_from_the_last_command(tmp_path):
    b = brain(tmp_path)
    b.is_app = lambda n: n.lower() in ("whatsapp", "safari")
    b.respond("open whatsapp")
    r = b.respond("close it after 10 seconds")
    assert r.fast and [(a.tool, a.args) for a in r.actions] == [("wait", {"seconds": 10}), ("app.close", {"name": "whatsapp"})]


def test_actions_nobody_asked_for_are_dropped():
    """The screenshot bug: a delay's "10" came back as "volume 10%"."""
    data = {"actions": [{"tool": "app.open", "args": {"name": "WhatsApp"}}, {"tool": "system.volume", "args": {"level": 0}},
                        {"tool": "app.close", "args": {"name": "WhatsApp"}}, {"tool": "system.volume", "args": {"level": 10}}],
            "reply": ""}
    i = parse_intent(data, "en", NOW, "open whatsapp and close it after 10 seconds")
    assert [a.tool for a in i.actions] == ["app.open", "app.close"]
    i = parse_intent(data, "en", NOW, "open whatsapp, mute it and close it")
    assert [a.tool for a in i.actions] == ["app.open", "system.volume", "app.close", "system.volume"]


def test_waits_are_kept_only_before_something():
    data = {"actions": [{"tool": "wait", "args": {"seconds": 5}}, {"tool": "wait", "args": {"seconds": "x"}},
                        {"tool": "app.close", "args": {"name": "Slack"}}, {"tool": "wait", "args": {"seconds": 3}}], "reply": ""}
    i = parse_intent(data, "en", NOW, "close slack in 5 seconds")
    assert [(a.tool, a.summary) for a in i.actions] == [("wait", "waiting 5 seconds"), ("app.close", "closing Slack")]


def test_a_delay_given_as_a_decimal_or_text_is_kept():
    # a model answering 10.0 used to lose the delay, so "close it after 10 seconds" closed at once
    for secs in (10.0, "10", "10.0"):
        data = {"actions": [{"tool": "app.open", "args": {"name": "WhatsApp"}}, {"tool": "wait", "args": {"seconds": secs}},
                            {"tool": "app.close", "args": {"name": "WhatsApp"}}], "reply": ""}
        i = parse_intent(data, "en", NOW, "open whatsapp and close it after 10 seconds")
        assert [(a.tool, a.args) for a in i.actions][1] == ("wait", {"seconds": 10}), secs
    for bad in (-5, True, "soon", float("nan")):
        data = {"actions": [{"tool": "wait", "args": {"seconds": bad}}, {"tool": "app.close", "args": {"name": "Slack"}}], "reply": ""}
        assert [a.tool for a in parse_intent(data, "en", NOW, "close slack later").actions] == ["app.close"]


def test_two_generated_scripts_become_one():
    # the second script used to be refused while the first waited for its confirmation
    data = {"actions": [{"tool": "mac.do", "args": {"task": "Add a reminder 'Buy milk' in the Reminders app."}},
                        {"tool": "mac.do", "args": {"task": "Play the playlist named 'Workout' in the Music app"}},
                        {"tool": "app.open", "args": {"name": "Safari"}}], "reply": ""}
    i = parse_intent(data, "en", NOW, "add buy milk to my reminders, play my workout playlist and open safari")
    assert [a.tool for a in i.actions] == ["mac.do", "app.open"]
    assert i.actions[0].args["task"] == ("Add a reminder 'Buy milk' in the Reminders app. "
                                         "Then: Play the playlist named 'Workout' in the Music app")
    assert i.actions[0].summary.startswith("doing “Add a reminder") and "Then: Play" in i.actions[0].summary
    assert data["actions"][0]["args"]["task"].endswith("app.")  # the model's own record is left as it was


def test_timer_descriptions_keep_the_seconds():
    from jarvis.llm.intents import Action, describe

    assert describe(Action("timer.set", {"seconds": 90}), "en", NOW) == "a timer for 1 minute 30 seconds"
    assert describe(Action("timer.set", {"seconds": 300}), "en", NOW) == "a 5-minute timer"
    assert describe(Action("timer.set", {"seconds": 5400}), "hi", NOW) == "1 घंटे 30 मिनट का टाइमर"


def test_the_command_prompt_fits_every_macs_context():
    """This morning's 8 GB change set a 2K context, but the prompt alone is ~3K tokens:
    Ollama then silently cut the start of the prompt, rules included."""
    history = [("en", "open whatsapp and close it after 10 seconds",
                '{"actions": [{"tool": "app.open", "args": {"name": "WhatsApp"}}], "reply": ""}')] * 4
    msgs = build_messages("Jarvis", "Aditya", NOW, "en", "x " * 60, history, ["a remembered fact " * 5] * 3)
    tokens = sum(len(m["content"]) for m in msgs) / 3.0  # conservative: English runs ~3.5-4 chars a token
    for p in (SMALL, STANDARD):
        assert tokens + 512 < p.num_ctx, (p.name, tokens)  # room for the answer
