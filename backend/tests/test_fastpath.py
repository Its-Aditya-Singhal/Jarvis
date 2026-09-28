"""Instant pattern-matched commands (no LLM) and what must still go to the LLM."""

from datetime import datetime

import pytest

from jarvis.llm.fastpath import parse_fast

NOW = datetime(2026, 9, 27, 15, 0)  # Sunday 3 PM
APPS = {"safari", "notes", "spotify", "saphari"}


def fast(text):
    r = parse_fast(text, "en", NOW, lambda n: n.lower() in APPS)
    return None if r is None else ([(a.tool, a.args) for a in r.actions], r.reply)


@pytest.mark.parametrize("text,seconds", [
    ("set a timer for 5 minutes", 300), ("Set a timer for five minutes.", 300), ("timer for 90 seconds", 90),
    ("5 minute ka timer laga do", 300), ("पांच मिनट का टाइमर लगाओ", 300), ("half an hour timer", 1800),
    ("set a timer for an hour and 10 minutes", 4200), ("timer for 2 minutes and 30 seconds", 150),
    ("remind me in 10 minutes", 600),
])
def test_timers(text, seconds):
    assert fast(text) == ([("timer.set", {"seconds": seconds})], "")


@pytest.mark.parametrize("text,when", [
    ("wake me up at 7", "2026-09-28T07:00"),            # wake-ups default to the morning
    ("set an alarm for 7:30 pm", "2026-09-27T19:30"),
    ("set an alarm for 6 tomorrow", "2026-09-28T06:00"),
    ("alarm for 5", "2026-09-27T17:00"),                # next 5 o'clock
    ("kal subah saat baje ka alarm laga do", "2026-09-28T07:00"),
    ("कल सुबह सात बजे का अलार्म लगा दो", "2026-09-28T07:00"),
    ("saade chhe baje ka alarm", "2026-09-27T18:30"),
])
def test_alarms(text, when):
    assert fast(text) == ([("alarm.set", {"time": when})], "")


def test_apps_only_when_installed():
    assert fast("open safari") == ([("app.open", {"name": "safari"})], "")
    assert fast("Safari kholo") == ([("app.open", {"name": "safari"})], "")
    assert fast("launch spotify please") == ([("app.open", {"name": "spotify"})], "")
    assert fast("open the pod bay doors") is None


def test_questions_answered_directly():
    assert fast("what time is it") == ([], "It's 3:00 PM.")
    assert fast("time kya hua hai") == ([], "It's 3:00 PM.")
    assert fast("what's the date today") == ([], "Today is Sunday, September 27, 2026.")


def test_other_tools_and_compounds():
    assert fast("take a note to call Dr. Mehta") == ([("notes.add", {"text": "Call Dr. Mehta"})], "")
    assert fast("What's on my calendar tomorrow?") == ([("calendar.list", {"date": "2026-09-28"})], "")
    assert fast("find files about lease") == ([("files.search", {"query": "lease"})], "")
    assert fast("cancel my alarms") == ([("alarm.cancel", {"kind": "alarm"})], "")
    assert fast("cancel the timer") == ([("alarm.cancel", {"kind": "timer"})], "")
    assert fast("wake me up at 7 tomorrow and add a note to buy milk") == (
        [("alarm.set", {"time": "2026-09-28T07:00"}), ("notes.add", {"text": "Buy milk"})], "")
    assert fast("open notes and set a timer for 2 minutes") == (
        [("app.open", {"name": "notes"}), ("timer.set", {"seconds": 120})], "")


@pytest.mark.parametrize("text", [
    "who wrote hamlet", "set a timer", "delete my note about milk", "what's the weather",
    "take a note to buy milk and eggs",   # ambiguous split: the LLM decides
    "remind me to call mom at 5", "नोट लिखो दूध लाना",  # free text in Devanagari isn't guessed
])
def test_everything_else_goes_to_the_llm(text):
    assert fast(text) is None


def test_brain_skips_the_model_for_fast_commands(settings):
    from jarvis.brain import Brain
    from jarvis.database.db import Database

    class NoModel:
        def chat_json(self, *a, **k):
            raise AssertionError("LLM must not be called")

    b = Brain(settings, Database(":memory:"), names=lambda: ("Friday", "A"), voice_gender=lambda: "female",
              client=NoModel(), clock=lambda: NOW)
    r = b.respond("set a timer for 5 minutes")
    assert r.fast and r.ok and r.actions[0].tool == "timer.set" and r.latency_s < 0.05
