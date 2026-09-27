"""Recent and dated files ("the PDF I downloaded yesterday"), revealing one in Finder,
and moving exactly one confirmed file to the Trash."""

import os
from datetime import datetime
from pathlib import Path

import pytest

from jarvis.database.db import Database
from jarvis.llm.fastpath import parse_fast
from jarvis.llm.intents import TOOLS, Action
from jarvis.security.crypto import StaticKeyProvider
from jarvis.tools.apps import AppIndex
from jarvis.tools.files import FileSearch, FolderError
from jarvis.tools.mac import MacControl
from jarvis.tools.runner import LEVELS, ToolRunner
from jarvis.tools.store import ToolStore

NOW = datetime(2026, 9, 27, 16, 30)  # Sunday
NEW = ("files.recent", "files.reveal", "files.trash")
MTIME = lambda st: st.st_mtime  # on a Mac, creation time would be when the test made the file


def touch(path: Path, when: datetime) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (when.timestamp(), when.timestamp()))
    return path


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    docs, dl = home / "Documents", home / "Downloads"
    touch(dl / "invoice.pdf", datetime(2026, 9, 26, 16, 12))
    touch(dl / "older.pdf", datetime(2026, 9, 26, 9, 0))
    touch(dl / "photo.jpg", datetime(2026, 9, 26, 10, 0))
    touch(dl / "today.zip", datetime(2026, 9, 27, 11, 0))
    touch(dl / ".hidden.pdf", datetime(2026, 9, 26, 12, 0))
    touch(dl / "Tool.app" / "inside.pdf", datetime(2026, 9, 26, 12, 0))  # never looked inside apps
    touch(docs / "Screenshot 2026-09-27 at 10.00.png", datetime(2026, 9, 27, 10, 0))
    touch(docs / "report.pdf", datetime(2026, 9, 20, 10, 0))
    (tmp_path / "outside.pdf").write_bytes(b"x")
    db = Database(":memory:")
    trashed: list[Path] = []
    revealed: list[str] = []
    files = FileSearch(db, trasher=trashed.append, stamp=MTIME)
    files.reveal = revealed.append  # type: ignore[method-assign]
    db.set("file_search_folders", f'["{docs}", "{dl}"]')
    runner = ToolRunner(db, ToolStore(db, StaticKeyProvider()), AppIndex([tmp_path]), files, None,
                        clock=lambda: NOW, mac=MacControl(home=home))
    return runner, trashed, revealed, home


def names(res) -> list[str]:
    return [Path(f).name for f in res.data["files"]]


def test_the_new_tools_are_registered_with_levels():
    for tool in NEW:
        assert tool in TOOLS and tool in LEVELS
    assert LEVELS["files.recent"] == 1 and LEVELS["files.reveal"] == 2 and LEVELS["files.trash"] == 3


def test_the_pdf_i_downloaded_yesterday(env):
    runner, *_ = env
    r = runner.run(Action("files.recent", {"kind": "pdf", "when": "yesterday", "folder": "downloads"}), "en")
    assert r.ok and names(r) == ["invoice.pdf", "older.pdf"]  # newest first; hidden and app contents skipped
    assert r.say.startswith("I found 2. The newest is “invoice.pdf”,") and "4:12 PM" in r.say


def test_recent_files_filters(env):
    runner, *_ = env
    run = lambda **a: runner.run(Action("files.recent", a), "en")
    assert names(run(kind="screenshot")) == ["Screenshot 2026-09-27 at 10.00.png"]
    assert names(run(when="today")) == ["today.zip", "Screenshot 2026-09-27 at 10.00.png"]
    assert names(run(kind="pdf", when="this week"))[:2] == ["invoice.pdf", "older.pdf"]
    assert "report.pdf" in names(run(kind="pdf", when="last week"))
    assert names(run(kind="pdf", when="2026-09-20")) == ["report.pdf"]
    assert names(run(kind="pdf", query="invoice")) == ["invoice.pdf"]
    none = run(kind="video", when="today")
    assert none.ok and none.say == "I didn't find any videos from today." and not none.data["files"]
    assert not run(when="the day after the big meeting").ok
    assert not run(folder="library").ok  # not an allowed folder


def test_reveal_uses_the_file_just_found(env):
    runner, _, revealed, _ = env
    assert not runner.run(Action("files.reveal", {}), "en").ok  # nothing found yet
    runner.run(Action("files.recent", {"kind": "pdf", "when": "yesterday", "folder": "downloads"}), "en")
    r = runner.run(Action("files.reveal", {}), "en")
    assert r.ok and r.say == "Showing “invoice.pdf” in Finder." and revealed[-1].endswith("invoice.pdf")
    r = runner.run(Action("files.reveal", {"kind": "screenshot"}), "en")
    assert r.ok and revealed[-1].endswith(".png")


def test_trash_is_planned_then_moves_exactly_that_file(env):
    runner, trashed, _, home = env
    plan = runner.plan(Action("files.trash", {"kind": "pdf", "when": "yesterday", "folder": "downloads"}), "en")
    assert getattr(plan, "path", "").endswith("invoice.pdf")
    assert plan.what == "“invoice.pdf” from Downloads" and not trashed  # nothing moved before confirmation
    res = runner.execute(plan)
    assert res.ok and trashed == [(home / "Downloads" / "invoice.pdf").resolve()]
    assert "Trash" in res.say and "put it back" in res.say


def test_trash_refuses_anything_but_one_plain_allowed_file(env, tmp_path):
    runner, trashed, _, home = env
    fs = runner.files
    dl = home / "Downloads"
    (dl / "link.pdf").symlink_to(tmp_path / "outside.pdf")
    (dl / "folder").mkdir()
    for bad, why in [(tmp_path / "outside.pdf", "outside"), (dl / "link.pdf", "symlink"), (dl / "folder", "only single files"),
                     (dl / "Tool.app", "only single files"), (dl / ".hidden.pdf", "hidden"), (dl / "gone.pdf", "no longer there")]:
        with pytest.raises(FolderError, match=why):
            fs.trash(bad)
    assert not trashed


def test_trash_rechecks_the_file_when_confirmed(env):
    runner, trashed, _, home = env
    runner.run(Action("files.recent", {"kind": "archive"}), "en")
    plan = runner.plan(Action("files.trash", {}), "en")
    (home / "Downloads" / "today.zip").unlink()  # removed between the question and the "yes"
    res = runner.execute(plan)
    assert not res.ok and "no longer there" in res.say and not trashed


def test_hindi_replies(env):
    runner, *_ = env
    r = runner.run(Action("files.recent", {"kind": "pdf", "when": "yesterday", "folder": "downloads"}), "hi")
    assert r.ok and "“invoice.pdf”" in r.say and "फ़ाइलें मिलीं" in r.say
    plan = runner.plan(Action("files.trash", {}), "hi")
    assert plan.what == "Downloads की फ़ाइल “invoice.pdf”"


def test_the_trash_asks_before_moving(settings, tmp_path):
    from tests.test_command_service import make

    svc, _, db, _ = make(settings, [Action("files.trash", {"kind": "pdf", "folder": "documents"})])
    docs = tmp_path / "Documents"
    touch(docs / "old.pdf", datetime(2026, 9, 1))
    trashed: list[Path] = []
    svc.tools.files = FileSearch(db, trasher=trashed.append, stamp=MTIME)
    svc.tools.mac = MacControl(home=tmp_path)
    db.set("file_search_folders", f'["{docs}"]')
    out = svc.command("move my latest pdf in documents to the trash")
    assert "Move “old.pdf” from Documents to the Trash?" in out["reply"] and not trashed
    res = svc.confirm(out["actions"][0]["data"]["pending"], True, "click")
    assert res["ok"] and trashed == [(docs / "old.pdf").resolve()]


@pytest.mark.parametrize("text,tool,args", [
    ("find the pdf i downloaded yesterday", "files.recent", {"kind": "pdf", "when": "yesterday", "folder": "downloads"}),
    ("what did i download today", "files.recent", {"kind": "any", "when": "today", "folder": "downloads"}),
    ("find my latest screenshot", "files.recent", {"kind": "screenshot"}),
    ("show me my recent downloads", "files.recent", {"kind": "any", "folder": "downloads"}),
    ("kal download ki hui pdf dikhao", "files.recent", {"kind": "pdf", "when": "yesterday", "folder": "downloads"}),
    ("show it in finder", "files.reveal", {}),
    ("show the pdf i downloaded yesterday in finder", "files.reveal", {"kind": "pdf", "when": "yesterday", "folder": "downloads"}),
    ("move it to the trash", "files.trash", {}),
    ("ise trash mein daal do", "files.trash", {}),
    ("delete the zip i downloaded today", "files.trash", {"kind": "archive", "when": "today", "folder": "downloads"}),
])
def test_fast_path(text, tool, args):
    r = parse_fast(text, "en", NOW)
    assert r is not None and [(a.tool, a.args) for a in r.actions] == [(tool, args)]


@pytest.mark.parametrize("text", ["delete it", "open the file", "find my documents", "delete the note about milk"])
def test_ambiguous_file_requests_go_to_the_model(text):
    assert parse_fast(text, "en", NOW) is None
