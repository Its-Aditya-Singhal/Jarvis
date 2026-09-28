"""Starts a local Ollama server when none is running.

Models live in a folder of the user's choosing (default ``~/Developer/ollama/
models``) via ``OLLAMA_MODELS``. The server binds to loopback only. A server
that was already running (e.g. ``brew services``) is used as-is and never
stopped by JARVIS.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx

from ..hardware import profile

log = logging.getLogger(__name__)

CANDIDATES = ["/opt/homebrew/bin/ollama", "/usr/local/bin/ollama", "/Applications/Ollama.app/Contents/Resources/ollama"]


def find_binary() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    return next((c for c in CANDIDATES if os.access(c, os.X_OK)), None)


class OllamaServer:
    def __init__(self, host: str, models_dir: Path | None, log_dir: Path):
        self.host = host
        self.models_dir = models_dir
        self.log_dir = log_dir
        self._proc: subprocess.Popen | None = None
        self.error: str | None = None
        # startup, the Models screen and a model pull can all ask at once: only one may start a server
        self._starting = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://{self.host}"

    def reachable(self) -> bool:
        try:
            return httpx.get(f"{self.url}/api/version", timeout=1.0).status_code == 200
        except httpx.HTTPError:
            return False

    def ensure(self, wait_s: float = 15.0) -> bool:
        """Make sure a server answers on ``host``; start one if needed."""
        if self.reachable():
            return True
        with self._starting:
            return self._ensure(wait_s)

    def _ensure(self, wait_s: float) -> bool:
        if self.reachable():  # another caller started it while this one waited
            self.error = None
            return True
        if self._proc is not None and self._proc.poll() is None:
            return self._wait(wait_s)  # ours is still starting up: never start a second one
        binary = find_binary()
        if binary is None:
            self.error = "Ollama is not installed (brew install ollama)"
            return False
        env = {**os.environ, "OLLAMA_HOST": self.host, "OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0",
               "OLLAMA_NUM_PARALLEL": str(profile().num_parallel)}  # 2: a background memory request never blocks a command
        if self.models_dir is not None:
            self.models_dir.mkdir(parents=True, exist_ok=True)
            env["OLLAMA_MODELS"] = str(self.models_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        logf = open(self.log_dir / "ollama.log", "ab")
        try:
            self._proc = subprocess.Popen([binary, "serve"], env=env, stdout=logf, stderr=logf, start_new_session=True)
        except OSError as exc:
            self.error = f"could not start Ollama: {exc}"
            return False
        finally:
            logf.close()
        return self._wait(wait_s)

    def _wait(self, wait_s: float) -> bool:
        assert self._proc is not None
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.reachable():
                log.info("started ollama (models in %s)", self.models_dir)
                self.error = None
                return True
            if self._proc.poll() is not None:
                self.error = "Ollama server exited while starting (see ollama.log)"
                return False
            time.sleep(0.3)
        self.error = "Ollama server is still starting (see ollama.log)"
        return False

    def stop(self) -> None:
        """Stops the server only if JARVIS started it."""
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
