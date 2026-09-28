"""Screen & display (screenshot, brightness, dark mode, System Settings pages) and clipboard & typing."""

import subprocess
from datetime import datetime

import pytest

from jarvis.database.db import Database
from jarvis.llm.intents import TOOLS, Action
from jarvis.security.crypto import StaticKeyProvider
from jarvis.tools.mac import SETTINGS_PAGES, SETTINGS_TITLES, SETTINGS_URL, MacControl, settings_page
from jarvis.tools.runner import LEVELS, ToolRunner
from jarvis.tools.store import ToolStore

NOW = datetime(2026, 9, 27, 16, 30)
NEW = ("screen.shot", "display.brightness", "display.dark_mode", "settings.open", "clipboard.read", "clipboard.note",
       "text.type")


class FakeClipboard:
    def __init__(self, text=None, concealed=False):
        self.value, self.hidden, self.history = text, concealed, []

    def text(self): return self.value
    def concealed(self): return self.hidden
    def set_text(self, t): self.value = t; self.hidden = False; self.history.append(t)


class FakeBrightness:
    def __init__(self, level=0.5, works=True):
        self.level, self.works = level, works

    def get(self): return self.level if self.works else None

    def set(self, v):
        if self.works:
            self.level = v
        return self.works


@pytest.fixture
def rig(tmp_path):
    calls, state = [], {"dark": False, "front": "Notes", "access": True, "paste_error": None}
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)

    def run(argv):
        calls.append(argv)
        if argv[:3] == ["defaults", "read", "-g"]:
            if not state["dark"]:
                raise subprocess.CalledProcessError(1, argv)  # the key is missing in light mode
            return "Dark\n"
        if argv[:2] == ["defaults", "read"]:
            raise subprocess.CalledProcessError(1, argv)
        if argv[0] == "osascript" and "keystroke" in argv[2] and state["paste_error"]:
            raise subprocess.CalledProcessError(1, argv, stderr=state["paste_error"])
        if argv[0] == "screencapture" and not argv[-1].startswith("-"):
            open(argv[-1], "wb").close()
        return ""

    clip, bright = FakeClipboard("old clip"), FakeBrightness()
    mac = MacControl(runner=run, running=lambda: [], home=home, clipboard=clip, front=lambda: state["front"],
                     brightness=bright, screen_access=lambda: state["access"])
    mac._sleep = lambda s: None
    db = Database(":memory:")
    r = ToolRunner(db, ToolStore(db, StaticKeyProvider()), None, None, None, mac=mac, clock=lambda: NOW)
    return r, calls, state, clip, bright, home


def test_new_tools_are_catalogued_with_levels():
    for t in NEW:
        assert t in TOOLS and t in LEVELS
    # everyday and reading: no voice match needed; the rest acts on apps or data (nothing here deletes)
    everyday = {"clipboard.read", "screen.shot", "display.brightness", "display.dark_mode"}
    assert all(LEVELS[t] == (1 if t in everyday else 2) for t in NEW)


def test_screenshot_saves_to_the_desktop_silently(rig):
    r, calls, state, *_, home = rig
    res = r.run(Action("screen.shot", {}), "en")
    assert res.ok and res.say == "Screenshot saved to your Desktop."
    assert calls[-1][:2] == ["screencapture", "-x"] and calls[-1][2].startswith(str(home / "Desktop" / "Screenshot 2"))
    again = r.run(Action("screen.shot", {}), "en")  # never overwrites an earlier one
    assert again.data["path"] != res.data["path"]
    assert r.run(Action("screen.shot", {"to": "clipboard"}), "en").say == "Screenshot copied to the clipboard."
    assert calls[-1] == ["screencapture", "-x", "-c"]


def test_screenshot_without_permission_explains_where_to_allow_it(rig):
    r, calls, state, *_ = rig
    state["access"] = False
    res = r.run(Action("screen.shot", {}), "en")
    assert not res.ok and "Screen Recording" in res.say and not any(c[0] == "screencapture" for c in calls)


def test_brightness_read_set_and_clamped(rig):
    r, _, _, _, bright, _ = rig
    assert r.run(Action("display.brightness", {}), "en").say == "Brightness is at 50%."
    assert r.run(Action("display.brightness", {"change": 10}), "en").say == "Brightness set to 60%."
    assert bright.level == pytest.approx(0.6)
    assert r.run(Action("display.brightness", {"level": 400}), "en").data["level"] == 100
    assert r.run(Action("display.brightness", {"change": -150}), "en").data["level"] == 0
    bright.works = False  # an external monitor
    assert r.run(Action("display.brightness", {"change": 10}), "en").say == "I can't control this screen's brightness."


def test_dark_mode_on_off_toggle(rig):
    r, calls, state, *_ = rig
    res = r.run(Action("display.dark_mode", {"on": True}), "en")
    assert res.ok and res.say == "Dark mode is on." and calls[-1][0] == "osascript" and calls[-1][-1] == "on"
    state["dark"] = True
    n = len(calls)
    assert r.run(Action("display.dark_mode", {"on": "true"}), "en").say == "Dark mode is already on."
    assert all(c[0] != "osascript" for c in calls[n:])
    assert r.run(Action("display.dark_mode", {}), "en").say == "Light mode is on."  # toggled
    assert calls[-1][-1] == "off"


def test_settings_pages_come_from_a_fixed_list(rig):
    r, calls, *_ = rig
    res = r.run(Action("settings.open", {"page": "wifi"}), "en")
    assert res.say == "Opening Wi-Fi in System Settings." and calls[-1] == ["open", SETTINGS_URL + "com.apple.wifi-settings-extension"]
    assert r.run(Action("settings.open", {"page": "the Bluetooth settings"}), "en").data["page"] == "com.apple.BluetoothSettings"
    assert r.run(Action("settings.open", {"page": "system settings"}), "en").say == "Opening System Settings."
    res = r.run(Action("settings.open", {"page": "x-apple.systempreferences:../../evil"}), "en")
    assert res.data["page"] is None and calls[-1] == ["open", SETTINGS_URL]  # unknown: just System Settings
    assert settings_page("privacy & security") == settings_page("privacy security") == "com.apple.settings.PrivacySecurity.extension"
    assert all(p in SETTINGS_TITLES for p in SETTINGS_PAGES.values())  # every page has a spoken name


def test_clipboard_read_and_passwords_are_never_read(rig):
    r, _, _, clip, _, _ = rig
    clip.value = "Flight PNR is X7K2QD"
    assert r.run(Action("clipboard.read", {}), "en").say == "Your clipboard says “Flight PNR is X7K2QD”."
    clip.value = None
    assert not r.run(Action("clipboard.read", {}), "en").ok
    clip.value, clip.hidden = "hunter2", True
    res = r.run(Action("clipboard.read", {}), "en")
    assert not res.ok and "hunter2" not in res.say and "password" in res.say


def test_clipboard_saved_as_a_note(rig):
    r, _, _, clip, _, _ = rig
    clip.value = "  Wifi password is on the router  "
    res = r.run(Action("clipboard.note", {}), "en")
    assert res.ok and res.tool == "clipboard.note" and res.say == "Saved your clipboard as a note."
    assert [n.text for n in r.store.notes()] == ["Wifi password is on the router"]
    clip.hidden = True
    assert not r.run(Action("clipboard.note", {}), "en").ok and len(r.store.notes()) == 1


def test_typing_pastes_then_restores_the_clipboard(rig):
    r, calls, state, clip, _, _ = rig
    res = r.run(Action("text.type", {"text": "On my way!"}), "en")
    assert res.ok and res.say == "Typed it into Notes."
    assert clip.history == ["On my way!", "old clip"] and clip.value == "old clip"
    assert calls[-1] == ["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down']


def test_typing_refuses_to_type_into_itself_and_explains_permissions(rig):
    r, calls, state, clip, _, _ = rig
    state["front"] = "JARVIS"
    res = r.run(Action("text.type", {"text": "hello"}), "en")
    assert not res.ok and "Switch to the app" in res.say and clip.history == []
    state["front"], state["paste_error"] = "Slack", "execution error: System Events got an error: osascript is not allowed to send keystrokes. (1002)"
    res = r.run(Action("text.type", {"text": "hello"}), "en")
    assert not res.ok and "Accessibility" in res.say and clip.value == "old clip"  # clipboard put back anyway


def test_a_copied_password_is_not_put_back_after_typing(rig):
    r, _, _, clip, _, _ = rig
    clip.value, clip.hidden = "hunter2", True
    assert r.run(Action("text.type", {"text": "hello"}), "en").ok
    assert clip.history == ["hello"]


def test_hindi_replies(rig):
    r, *_ = rig
    assert r.run(Action("display.brightness", {"level": 30}), "hi").say == "ब्राइटनेस 30% कर दी है।"
    assert r.run(Action("settings.open", {"page": "bluetooth"}), "hi").say == "Bluetooth खोल दिया है।"
