"""The Ollama server the backend may start: never two of them, never left behind."""

import os
import stat
import time

import pytest

from jarvis.llm import server as srv


@pytest.fixture
def slow_ollama(tmp_path, monkeypatch):
    """An `ollama` that starts but never answers (a slow first start)."""
    pids = tmp_path / "pids"
    exe = tmp_path / "ollama"
    exe.write_text(f"#!/bin/sh\necho $$ >> {pids}\nexec sleep 30\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(srv, "find_binary", lambda: str(exe))
    return pids


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _until(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            return False
        time.sleep(0.02)
    return True


def test_a_slow_server_is_waited_for_not_started_twice(tmp_path, slow_ollama):
    s = srv.OllamaServer("127.0.0.1:9", None, tmp_path / "logs")  # nothing answers on port 9
    assert not s.ensure(wait_s=0.3)
    assert not s.ensure(wait_s=0.3)
    assert _until(slow_ollama.exists)  # the fake server has started (slow when the machine is busy)
    time.sleep(0.2)  # room for a wrongly started second one to show up
    pids = [int(p) for p in slow_ollama.read_text().split()]
    assert len(pids) == 1, "a second `ollama serve` was started while the first was still coming up"
    s.stop()
    assert _until(lambda: not _alive(pids[0]))


def test_the_server_is_stopped_when_the_desktop_app_disappears(settings, monkeypatch):
    """If the shell dies, the backend exits on its own: our Ollama server must go with it."""
    from jarvis import __main__ as main

    stopped = []
    brain = type("B", (), {"stop": lambda self: stopped.append("stop"), "free_memory": lambda self: 0})()
    app = type("A", (), {"state": type("S", (), {"svc": type("V", (), {"brain": brain})()})()})()
    main._on_parent_exit([app])
    assert stopped == ["stop"]
    main._on_parent_exit([])  # before the app exists: nothing to do
