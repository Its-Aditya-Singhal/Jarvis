"""Everyday Mac controls (quit apps, folders, web, volume, media, battery, lock) and memory upkeep."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from jarvis.database.db import Database
from jarvis.llm.fastpath import parse_fast
from jarvis.llm.intents import TOOLS, Action
from jarvis.security.crypto import StaticKeyProvider
from jarvis.tools.mac import MacControl, clean_name
from jarvis.tools.runner import LEVELS, ToolRunner
from jarvis.tools.store import ToolStore

NOW = datetime(2026, 9, 27, 16, 30)


class FakeApp:
    def __init__(self, name):
        self.name, self.quit = name, False

    def localizedName(self): return self.name
    def terminate(self): self.quit = True; return True


@pytest.fixture
def mac(tmp_path):
    calls = []
    apps = [FakeApp(n) for n in ("Finder", "Safari", "‎WhatsApp", "Spotify")]
    docs = tmp_path / "Projects"
    docs.mkdir()
    home = tmp_path / "home"  # a fake home, so the tests don't depend on this machine's folders
    (home / "Documents").mkdir(parents=True)

    def run(argv):
        calls.append(argv)
        if argv[:3] == ["osascript", "-e", "get volume settings"]:
            return "output volume:40, input volume:50, alert volume:100, output muted:false"
        if argv[:2] == ["pmset", "-g"]:
            return "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t56%; discharging"
        return ""

    return MacControl(runner=run, running=lambda: apps, extra_folders=lambda: [docs], home=home), calls, apps, docs


@pytest.fixture
def runner(mac):
    db = Database(":memory:")
    r = ToolRunner(db, ToolStore(db, StaticKeyProvider()), None, None, None, mac=mac[0], clock=lambda: NOW)
    return r, mac


def test_every_new_tool_is_catalogued_with_a_level():
    for t in ("app.close", "folder.open", "web.open", "system.volume", "media.control", "system.battery",
              "system.lock", "alarm.list"):
        assert t in TOOLS and t in LEVELS
    assert LEVELS["system.battery"] == 1 and LEVELS["app.close"] == 2


def test_quit_matches_running_apps_politely(runner):
    r, (m, calls, apps, _) = runner
    res = r.run(Action("app.close", {"name": "safari"}), "en")
    assert res.ok and res.say == "Closing Safari." and apps[1].quit
    assert r.run(Action("app.close", {"name": "whatsapp"}), "en").say == "Closing WhatsApp."  # invisible mark dropped
    assert not r.run(Action("app.close", {"name": "Xcode"}), "en").ok  # not running
    assert not r.run(Action("app.close", {"name": "finder"}), "en").ok and not apps[0].quit  # never quit
    assert clean_name("‎WhatsApp") == "WhatsApp"


def test_open_folders_known_and_allowed(runner, tmp_path):
    r, (m, calls, _, docs) = runner
    res = r.run(Action("folder.open", {"name": "my documents folder"}), "en")
    assert res.ok and calls[-1][0] == "open" and calls[-1][1].endswith("/Documents")
    assert r.run(Action("folder.open", {"name": "projects"}), "en").data["path"] == str(docs)
    assert not r.run(Action("folder.open", {"name": "secret stuff"}), "en").ok
    (docs / "Capstone").mkdir()
    assert r.run(Action("folder.open", {"name": "capstone folder"}), "en").data["path"] == str(docs / "Capstone")
    (docs / ".hidden").mkdir()
    assert not r.run(Action("folder.open", {"name": ".hidden"}), "en").ok


def test_app_open_falls_back_to_folders_and_sites(runner):
    r, (m, calls, _, _) = runner
    r.apps = SimpleNamespace(open=lambda name: None)
    assert r.run(Action("app.open", {"name": "Documents"}), "en").tool == "folder.open"
    res = r.run(Action("app.open", {"name": "youtube"}), "en")
    assert res.tool == "web.open" and calls[-1] == ["open", "https://www.youtube.com"]


def test_web_open_only_opens_web_addresses(runner):
    r, (m, calls, _, _) = runner
    assert r.run(Action("web.open", {"target": "github.com"}), "en").data["url"] == "https://github.com"
    res = r.run(Action("web.open", {"target": "best laptops 2026"}), "en")
    assert res.data["url"] == "https://www.google.com/search?q=best+laptops+2026"
    assert m.site_url("file:///etc/passwd")[0].startswith("https://www.google.com/search")
    with pytest.raises(ValueError):
        m.open_url("file:///etc/passwd")


def test_volume_media_battery_lock(runner):
    r, (m, calls, _, _) = runner
    assert r.run(Action("system.volume", {"change": 10}), "en").say == "Volume set to 50%."
    assert calls[-1][-1] == "50"  # passed as argv, never interpolated
    assert r.run(Action("system.volume", {"level": 250}), "en").say == "Volume set to 100%."
    assert r.run(Action("system.volume", {"mute": True}), "en").say == "Muted."
    assert r.run(Action("system.volume", {}), "en").say == "Volume is at 40%."
    assert r.run(Action("media.control", {"action": "next"}), "en").say == "Next track on Spotify."
    assert calls[-1][-1] == "Spotify"
    assert r.run(Action("system.battery", {}), "en").say == "Battery is at 56%, on battery."
    r.run(Action("system.lock", {}), "en")
    assert calls[-1] == ["pmset", "displaysleepnow"]


def test_alarm_list(runner):
    r, _ = runner
    assert r.run(Action("alarm.list", {}), "en").say == "You have no alarms or timers set."
    r.store.add_alarm("alarm", NOW + timedelta(hours=15), "")
    r.store.add_alarm("timer", NOW + timedelta(minutes=4, seconds=30), "")
    say = r.run(Action("alarm.list", {}), "en").say
    assert say == "You have a timer with 4 min 30 s left and an alarm at 7:30 AM tomorrow."


@pytest.mark.parametrize("text,tool,args", [
    ("Open documents folder", "folder.open", {"name": "documents"}),
    ("open my downloads", "folder.open", {"name": "downloads"}),
    ("open music folder", "folder.open", {"name": "music"}),
    ("open youtube", "web.open", {"target": "youtube"}),
    ("go to github.com", "web.open", {"target": "github.com"}),
    ("Close Safari", "app.close", {"name": "safari"}),
    ("safari band karo", "app.close", {"name": "safari"}),
    ("volume up", "system.volume", {"change": 10}),
    ("set volume to 40", "system.volume", {"level": 40}),
    ("mute", "system.volume", {"mute": True}),
    ("pause the music", "media.control", {"action": "pause"}),
    ("agla gaana", "media.control", {"action": "next"}),
    ("how much battery do i have", "system.battery", {}),
    ("lock the screen", "system.lock", {}),
    ("what alarms do i have", "alarm.list", {}),
    ("google best laptops", "web.open", {"target": "Best laptops"}),
])
def test_fast_path_everyday_commands(text, tool, args):
    r = parse_fast(text, "en", NOW, lambda n: n.lower() in {"safari", "music"})
    assert r is not None and [(a.tool, a.args) for a in r.actions] == [(tool, args)]


@pytest.mark.parametrize("text", ["Tell me the time.", "What is the time right now?", "Current time",
                                  "Can you tell me the time", "abhi kitne baje hain", "do you know the time"])
def test_every_way_of_asking_the_time_is_instant(text):
    r = parse_fast(text, "en", NOW)
    assert r is not None and r.reply == "It's 4:30 PM."


def test_open_music_is_the_app_and_close_needs_an_installed_app():
    is_app = lambda n: n.lower() in {"music", "safari"}
    assert parse_fast("open music", "en", NOW, is_app).actions[0].tool == "app.open"
    assert parse_fast("close the door please", "en", NOW, is_app) is None
    assert parse_fast("search for my passport", "en", NOW, is_app) is None  # could be files: the LLM decides


# -- memory upkeep ----------------------------------------------------------------------
class FakeClient:
    def __init__(self, loaded):
        self._loaded, self.unloaded = dict(loaded), []

    def loaded(self): return dict(self._loaded)
    def unload(self, m): self.unloaded.append(m); self._loaded.pop(m, None)


def test_only_the_active_chat_model_stays_loaded(settings):
    from jarvis.brain import Brain

    b = Brain(settings, Database(":memory:"), names=lambda: ("Friday", "A"), voice_gender=lambda: "female")
    b.client = FakeClient({"qwen2.5:7b": 4 << 30, "qwen2.5:3b": 2 << 30, "bge-m3:latest": 1 << 29})
    b.override = "qwen2.5:3b"  # Fast mode
    assert b.tidy() == 4 << 30 and b.client.unloaded == ["qwen2.5:7b"]
    assert b.free_memory() == (2 << 30) + (1 << 29) and b.client.loaded() == {}


def test_low_memory_unloads_models_only_when_idle(settings):
    from test_command_service import make

    svc, *_ = make(settings, [])
    freed = []
    svc.brain.free_memory = lambda: freed.append(1) or 3 << 30
    svc.brain.last_used = __import__("time").monotonic()
    svc._check_memory({"system_mem_pct": 95, "ollama_mb": 4000})
    assert freed == []  # just used: keep it
    svc.brain.last_used -= 300
    svc._check_memory({"system_mem_pct": 70, "ollama_mb": 4000})
    assert freed == []  # plenty of memory
    svc._check_memory({"system_mem_pct": 95, "ollama_mb": 4000})
    assert freed == [1]


def test_quitting_unloads_only_jarvis_models(settings):
    from jarvis.brain import Brain

    b = Brain(settings, Database(":memory:"), names=lambda: ("Friday", "A"), voice_gender=lambda: "female")
    b.client = FakeClient({"qwen2.5:7b": 4 << 30, "bge-m3:latest": 1 << 29, "llama3:8b": 5 << 30})
    b.server = SimpleNamespace(stop=lambda: None)
    b.stop()
    assert sorted(b.client.unloaded) == ["bge-m3:latest", "qwen2.5:7b"]  # someone else's model stays
