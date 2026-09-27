"""Tools: encrypted store, runner, alarms, apps, files, Apple bridge (all offline)."""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from jarvis.database.db import Database
from jarvis.llm.intents import Action
from jarvis.security.crypto import StaticKeyProvider
from jarvis.tools.apple import AppleBridge, AppleError, AppleEvent
from jarvis.tools.apps import AppIndex
from jarvis.tools.files import FileSearch, FolderError
from jarvis.tools.runner import ToolRunner
from jarvis.tools.scheduler import AlarmScheduler
from jarvis.tools.store import ToolStore

NOW = datetime(2026, 9, 27, 21, 30)


class FakeApple:
    def __init__(self, fail=False):
        self.fail, self.created, self.notes = fail, [], []

    def _check(self):
        if self.fail:
            raise AppleError("not allowed to control the app")

    def create_event(self, cal, title, start, end):
        self._check()
        self.created.append((cal, title, start))
        return f"uid-{len(self.created)}"

    def events_on(self, day):
        self._check()
        return [AppleEvent("Dentist", datetime.combine(day, datetime.min.time()).replace(hour=10), "Home", "apple-1")]

    def create_note(self, text):
        self._check()
        self.notes.append(text)
        return f"note-{len(self.notes)}"

    def search_notes(self, q, limit=5):
        self._check()
        return ["Shopping list"] if "shop" in q.lower() else []


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    store = ToolStore(db, StaticKeyProvider())
    apps_dir = tmp_path / "Applications"
    for name in ("Safari", "Visual Studio Code", "Notes"):
        (apps_dir / f"{name}.app").mkdir(parents=True)
    opened = []
    apps = AppIndex([apps_dir], opener=opened.append)
    home_docs = tmp_path / "docs"
    home_docs.mkdir()
    files = FileSearch(db, runner=lambda folder, q: [] if q.endswith("invoices") else [
        str(folder / "tax_invoice_2025.pdf"), str(folder / ".secret/x.pdf"), str(folder / "notes.txt")])
    db.set("file_search_folders", f'["{home_docs}"]')
    apple = FakeApple()
    runner = ToolRunner(db, store, apps, files, apple, clock=lambda: NOW)
    return runner, store, db, apple, opened


def run(runner, tool, lang="en", **args):
    return runner.run(Action(tool, args), lang)


def test_personal_text_is_encrypted_at_rest(env, tmp_path):
    runner, store, db, *_ = env
    run(runner, "notes.add", text="bank PIN hint: blue elephant")
    run(runner, "calendar.create", title="Therapy session", start="2026-09-28T10:00")
    raw = (tmp_path / "t.sqlite3").read_bytes()
    assert b"blue elephant" not in raw and b"Therapy" not in raw
    assert store.notes()[0].text == "bank PIN hint: blue elephant"


def test_alarm_and_timer(env):
    runner, store, *_ = env
    r = run(runner, "alarm.set", time="2026-09-28T07:00")
    assert r.ok and r.say == "Alarm set for 7:00 AM tomorrow."
    assert not run(runner, "alarm.set", time="2026-09-27T07:00").ok  # already passed
    assert not run(runner, "alarm.set", time="soon").ok
    r = run(runner, "timer.set", "hinglish", seconds=600)
    assert r.ok and r.say == "10 मिनट का टाइमर शुरू कर दिया है।"
    assert not run(runner, "timer.set", seconds=-5).ok
    assert [a.kind for a in store.alarms()] == ["timer", "alarm"]


def test_calendar_with_and_without_apple_sync(env):
    runner, store, db, apple, _ = env
    r = run(runner, "calendar.create", title="Team meeting", start="2026-09-28T17:00")
    assert r.ok and "Apple" not in r.say and apple.created == []
    db.set("apple_calendar_sync", "1")
    db.set("apple_calendar_name", "Work")
    r = run(runner, "calendar.create", title="Dinner with Riya", start="2026-09-28T20:00")
    assert r.ok and "also in Apple Calendar" in r.say and apple.created[0][:2] == ("Work", "Dinner with Riya")
    r = run(runner, "calendar.list", date="2026-09-28")
    assert "3 events tomorrow" in r.say and "Dentist at 10:00 AM" in r.say  # local + Apple, time-ordered
    apple.fail = True
    r = run(runner, "calendar.create", title="Gym", start="2026-09-29T07:00")
    assert r.ok and "sync failed" in r.say  # still saved locally
    assert run(runner, "calendar.list", date="2026-10-05").say.startswith("Your calendar is clear")


def test_notes_search_local_and_apple(env):
    runner, store, db, apple, _ = env
    run(runner, "notes.add", text="Buy milk and eggs on the way home")
    run(runner, "notes.add", text="Call the bank about the card")
    r = run(runner, "notes.search", query="milk")
    assert r.data["notes"] == ["Buy milk and eggs on the way home"]
    db.set("apple_notes_sync", "1")
    run(runner, "notes.add", text="Pack gym bag")
    assert apple.notes == ["Pack gym bag"]
    assert run(runner, "notes.search", query="shopping").data["notes"] == ["Shopping list"]
    assert run(runner, "notes.search", "hi", query="xyz").say.startswith("“xyz” से जुड़ा कोई नोट नहीं")


def test_open_any_installed_app_only(env):
    runner, *_, opened = env
    assert run(runner, "app.open", name="vs code").say == "Opening Visual Studio Code."
    assert run(runner, "app.open", "hi", name="सफारी").ok
    r = run(runner, "app.open", name="rm -rf /")
    assert not r.ok and len(opened) == 2
    assert [p.name for p in opened] == ["Visual Studio Code.app", "Safari.app"]


def test_file_search_respects_folders(env, tmp_path):
    runner, store, db, *_ = env
    r = run(runner, "files.search", query="tax invoice")
    assert r.ok and [Path(f).name for f in r.data["files"]] == ["tax_invoice_2025.pdf", "notes.txt"]  # hidden skipped
    assert run(runner, "files.search", query="invoices").ok  # plural falls back to singular
    fs = runner.files
    assert fs.allowed(tmp_path / "docs" / "a.pdf") and not fs.allowed(tmp_path / "elsewhere.pdf")


def test_folder_validation():
    fs = FileSearch(Database(":memory:"))
    home = Path.home()
    for bad in [home, home / "Library", home / ".ssh", Path("/etc"), Path("/")]:
        with pytest.raises(FolderError):
            fs.validate(bad)
    assert fs.folders()[0].name == "Documents"  # defaults


def test_alarm_scheduler_rings_dismisses_snoozes_and_marks_missed(tmp_path):
    store = ToolStore(Database(tmp_path / "a.sqlite3"), StaticKeyProvider())
    clock = {"now": NOW}
    stale = store.add_alarm("alarm", NOW - timedelta(hours=2), "old")
    store.add_alarm("alarm", NOW + timedelta(minutes=1), "wake")
    rings = []
    sch = AlarmScheduler(store, on_ring=lambda a, n: rings.append((a.label, n)), clock=lambda: clock["now"], ring_every_s=0)
    sch.start()
    sch.stop()
    assert [a.id for a in sch.missed] == [stale]
    sch.tick()
    assert rings == []
    clock["now"] = NOW + timedelta(minutes=1, seconds=1)
    sch.tick()
    sch.tick()
    assert rings == [("wake", 0), ("wake", 1)] and sch.ringing()
    assert sch.snooze(5) == 1 and not sch.ringing()
    clock["now"] += timedelta(minutes=5, seconds=1)
    sch.tick()
    assert sch.ringing() and sch.dismiss() == 1
    assert store.alarms() == []


def test_applescript_gets_values_as_argv_not_code():
    seen = []
    bridge = AppleBridge(run=lambda script, *args: seen.append((script, args)) or "uid-9")
    evil = 'x" & (do shell script "rm -rf ~") & "'
    assert bridge.create_event("Home", evil, datetime(2026, 1, 1, 9), datetime(2026, 1, 1, 10)) == "uid-9"
    script, args = seen[0]
    assert evil not in script and args[1] == evil
    bridge.create_note("<b>hi</b> & bye")
    assert seen[1][1][1] == "&lt;b&gt;hi&lt;/b&gt; &amp; bye"
    assert AppleBridge(run=lambda s, *a: "Dinner\t2026-01-01T20:00:00\tHome\tu1\n").events_on(date(2026, 1, 1))[0].title == "Dinner"
