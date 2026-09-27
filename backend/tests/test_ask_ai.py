"""Asking Claude or ChatGPT: the prompt is written into a new chat, never sent."""

from datetime import datetime
from types import SimpleNamespace

import pytest
from test_screen_clipboard import FakeClipboard

from jarvis.database.db import Database
from jarvis.llm.fastpath import parse_fast
from jarvis.llm.intents import TOOLS, Action
from jarvis.security.crypto import StaticKeyProvider
from jarvis.tools.mac import MacControl
from jarvis.tools.runner import LEVELS, ToolRunner
from jarvis.tools.store import ToolStore

NOW = datetime(2026, 9, 27, 16, 30)
PASTE = ["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down']
NEW_CHAT = ["osascript", "-e", 'tell application "System Events" to keystroke "n" using command down']


@pytest.fixture
def rig(tmp_path):
    calls, state = [], {"front": "Safari", "installed": {"Claude", "ChatGPT"}, "comes_up": True, "allowed": True}
    apps_dir = tmp_path / "Applications"
    for n in ("Claude", "ChatGPT"):
        (apps_dir / f"{n}.app").mkdir(parents=True)

    def run(argv):
        calls.append(argv)
        if argv[0] == "open" and argv[1].endswith(".app") and state["comes_up"]:
            state["front"] = argv[1].rsplit("/", 1)[1].removesuffix(".app")
        if argv[0] == "osascript" and "keystroke" in argv[2] and not state["allowed"]:
            import subprocess
            raise subprocess.CalledProcessError(1, argv, stderr="osascript is not allowed to send keystrokes. (1002)")
        return ""

    def resolve(name):
        return (name, apps_dir / f"{name}.app") if name in state["installed"] else None

    clip = FakeClipboard("old clip")
    mac = MacControl(runner=run, running=lambda: [], home=tmp_path, clipboard=clip, front=lambda: state["front"])
    mac._sleep = lambda s: None
    db = Database(":memory:")
    r = ToolRunner(db, ToolStore(db, StaticKeyProvider()), SimpleNamespace(resolve=resolve), None, None, mac=mac,
                   clock=lambda: NOW)
    return r, calls, state, clip


def test_catalogued_at_level_2():
    assert "ai.ask" in TOOLS and LEVELS["ai.ask"] == 2


def test_claude_app_gets_a_new_chat_with_the_prompt_pasted_but_not_sent(rig):
    r, calls, state, clip = rig
    res = r.run(Action("ai.ask", {"service": "claude", "prompt": "Build a website for my bakery"}), "en")
    assert res.ok and res.data["via"] == "app"
    assert res.say == "Claude is open with your prompt “Build a website for my bakery”. Press Return to send it."
    assert calls[0][0] == "open" and calls[0][1].endswith("Claude.app")
    assert calls[1:] == [NEW_CHAT, PASTE]  # no Return: the owner sends it
    assert clip.history == ["Build a website for my bakery", "old clip"]


def test_without_the_app_claude_opens_on_the_web_pre_filled(rig):
    r, calls, state, _ = rig
    state["installed"] = set()
    res = r.run(Action("ai.ask", {"service": "claude", "prompt": "Explain recursion & give an example"}), "en")
    assert res.data["via"] == "web" and "in your browser" in res.say
    assert calls == [["open", "https://claude.ai/new?q=Explain%20recursion%20%26%20give%20an%20example"]]


def test_chatgpt_website_gets_the_prompt_through_the_clipboard(rig):
    r, calls, state, clip = rig
    state["installed"] = set()
    res = r.run(Action("ai.ask", {"service": "ChatGPT", "prompt": "Plan a trip to Goa"}), "en")
    assert res.data["via"] == "clipboard" and "Command-V" in res.say
    assert calls == [["open", "https://chatgpt.com/"]] and clip.value == "Plan a trip to Goa"  # ?q= would send it


def test_never_pastes_into_another_app_when_the_chat_app_doesnt_come_up(rig):
    r, calls, state, clip = rig
    state["comes_up"] = False
    res = r.run(Action("ai.ask", {"service": "chatgpt", "prompt": "Hello there"}), "en")
    assert res.data["via"] == "clipboard" and PASTE not in calls and NEW_CHAT not in calls


def test_without_accessibility_claude_falls_back_to_the_website(rig):
    r, calls, state, _ = rig
    state["allowed"] = False
    res = r.run(Action("ai.ask", {"service": "claude", "prompt": "Hello there"}), "en")
    assert res.data["via"] == "web" and calls[-1] == ["open", "https://claude.ai/new?q=Hello%20there"]


def test_only_known_services_and_a_prompt(rig):
    r, calls, _, _ = rig
    assert not r.run(Action("ai.ask", {"service": "claude", "prompt": "  "}), "en").ok
    res = r.run(Action("ai.ask", {"service": "evil; rm -rf", "prompt": "hi there"}), "en")
    assert res.data["service"] == "claude"  # anything unknown means Claude, never another app or URL


@pytest.mark.parametrize("text,service,prompt", [
    ("Open Claude and ask it to build a website for my bakery", "claude", "Build a website for my bakery"),
    ("ask claude to write a haiku about rain and snow", "claude", "Write a haiku about rain and snow"),
    ("Ask ChatGPT what is 15 plus 27", "chatgpt", "What is 15 plus 27"),
    ("claude se pucho ki python mein list kaise sort karte hain", "claude", "Python mein list kaise sort karte hain"),
    ("ask cloud to explain recursion", "claude", "Explain recursion"),
])
def test_fast_path(text, service, prompt):
    r = parse_fast(text, "en", NOW)
    assert r is not None and [(a.tool, a.args) for a in r.actions] == [("ai.ask", {"service": service, "prompt": prompt})]


def test_open_claude_alone_is_just_the_app():
    assert parse_fast("open claude", "en", NOW, lambda n: n == "claude").actions[0].tool == "app.open"
    assert parse_fast("ask claude", "en", NOW) is None
