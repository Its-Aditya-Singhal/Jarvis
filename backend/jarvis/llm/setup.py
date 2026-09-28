"""First-run help for the local language model: is Ollama installed and running,
which of JARVIS's models are pulled, and pulling the missing ones with progress.

Only the models JARVIS uses can be pulled from here. Ollama downloads them in
its own process (the backend's offline guard doesn't apply to it).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from .client import LLMUnavailable, OllamaClient
from .server import OllamaServer, find_binary

log = logging.getLogger(__name__)

INSTALL = {"url": "https://ollama.com/download/mac", "brew": "brew install ollama"}
# download sizes shown before pulling (Q4 weights)
SIZES_GB = {"qwen2.5:7b": 4.7, "qwen2.5:3b": 1.9, "qwen2.5:14b": 9.0, "bge-m3": 1.2, "llama3.2:3b": 2.0}


def _same(a: str, b: str) -> bool:
    return a == b or a.removesuffix(":latest") == b.removesuffix(":latest")


class OllamaSetup:
    def __init__(self, server: OllamaServer, client: OllamaClient,
                 wanted: Callable[[], list[dict]], on_change: Callable[[], None] = lambda: None):
        """``wanted()``: [{"name", "purpose", "required"}] — the models JARVIS uses."""
        self.server = server
        self.client = client
        self.wanted = wanted
        self.on_change = on_change
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pull: dict = {"model": None, "state": "idle", "status": "", "completed": 0, "total": 0, "error": None}

    def detect(self) -> dict:
        binary = find_binary()
        running = self.server.reachable()
        try:
            have = self.client.models() if running else []
        except LLMUnavailable:
            have = []
        models = [{**m, "installed": any(_same(h, m["name"]) for h in have), "size_gb": SIZES_GB.get(m["name"])}
                  for m in self.wanted()]
        with self._lock:
            pull = dict(self._pull)
        return {"installed": binary is not None or running, "running": running, "error": self.server.error,
                "install": INSTALL, "models": models, "pull": pull,
                "ready": running and all(m["installed"] for m in models if m["required"])}

    def start_server(self) -> dict:
        self.server.ensure()
        return self.detect()

    def pull(self, model: str) -> dict:
        if not any(_same(model, m["name"]) for m in self.wanted()):
            raise ValueError(f"{model} isn't one of the assistant's models")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {"started": False, "reason": f"already downloading {self._pull['model']}"}
            self._pull = {"model": model, "state": "downloading", "status": "starting", "completed": 0, "total": 0,
                          "error": None}
            self._thread = threading.Thread(target=self._run, args=(model,), name="ollama-pull", daemon=True)
            self._thread.start()
        self.on_change()
        return {"started": True}

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, model: str) -> None:
        def progress(status: str, completed: int, total: int) -> None:
            with self._lock:
                self._pull.update(status=status, **({"completed": completed, "total": total} if total else {}))
            self.on_change()

        try:
            if not self.server.ensure():
                raise LLMUnavailable(self.server.error or "Ollama isn't running")
            self.client.pull(model, progress)
            with self._lock:
                self._pull.update(state="done", status="success")
        except Exception as exc:  # anything: the screen must never show "downloading" forever
            log.warning("ollama pull %s failed: %s", model, exc)
            msg = str(exc) if isinstance(exc, LLMUnavailable | ValueError) else f"Download failed ({type(exc).__name__})"
            with self._lock:
                self._pull.update(state="error", error=f"{msg.rstrip('.')}. Press Download again to resume.")
        self.on_change()
