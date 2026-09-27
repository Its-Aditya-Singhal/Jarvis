"""Performance modes, the power source and resource use.

A mode picks the language model, the speech-recognition size, how often the
camera is analysed and whether memory suggestions run (each costs a second
model call). "auto" means Balanced on power and Fast on battery.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .netguard import observe_connections

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Mode:
    name: str
    llm: str  # "fast" -> the fast-mode model, "main" -> the owner's chosen model
    stt: str  # Whisper size
    face_fps: float
    suggestions: bool


MODES = {
    "fast": Mode("fast", "fast", "small", 4.0, False),
    "balanced": Mode("balanced", "main", "small", 6.0, True),
    "quality": Mode("quality", "main", "medium", 8.0, True),
}


def effective_mode(pref: str, on_battery: bool) -> str:
    if pref == "auto":
        return "fast" if on_battery else "balanced"
    return pref if pref in MODES else "balanced"


_BATT = re.compile(r"(\d+)%")


def loaded_models_mb(host: str) -> int | None:
    """Memory of the models Ollama has loaded (mostly GPU memory, which process RSS misses)."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}/api/ps", timeout=1.0) as r:
            models = json.load(r).get("models") or []
    except (OSError, ValueError):
        return None
    return round(sum(int(m.get("size") or 0) for m in models) / 2**20)


def read_power() -> tuple[bool, int | None]:
    """(on battery, charge %) from ``pmset``; desktops report no battery."""
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return False, None
    m = _BATT.search(out)
    return "Battery Power" in out, int(m.group(1)) if m else None


class PerfMonitor:
    """Samples the power source and resource use in the background."""

    def __init__(self, on_power_change: Callable[[bool], None], power: Callable[[], tuple[bool, int | None]] = read_power,
                 period_s: float = 5.0, ollama_host: str = "127.0.0.1:11434"):
        self.on_power_change = on_power_change
        self.on_sample: Callable[[dict], None] = lambda stats: None
        self.ollama_host = ollama_host
        self.power = power
        self.period_s = period_s
        self.on_battery = False
        self.battery_pct: int | None = None
        self.stats: dict = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: Any = None  # psutil.Process, created on the first sample

    def start(self) -> None:
        self.on_battery, self.battery_pct = self.power()
        self._thread = threading.Thread(target=self._loop, name="perf", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        n = 0
        while not self._stop.wait(self.period_s):
            n += 1
            try:
                self.stats = self.sample()
                self.on_sample(self.stats)
                if n % 4 == 0:  # the power source changes rarely: check every 20 s
                    on_batt, pct = self.power()
                    self.battery_pct = pct
                    if on_batt != self.on_battery:
                        self.on_battery = on_batt
                        self.on_power_change(on_batt)
            except Exception:
                log.exception("perf sample failed")

    def sample(self) -> dict:
        try:
            import psutil
        except ImportError:
            return {}
        if self._proc is None:
            self._proc = psutil.Process(os.getpid())
            self._proc.cpu_percent(None)  # first call primes the counter
        rss = self._proc.memory_info().rss
        vm = psutil.virtual_memory()
        return {
            "external": observe_connections(),
            "system_mem_gb": round(vm.total / 2**30),
            "cpu": round(self._proc.cpu_percent(None) / (psutil.cpu_count() or 1), 1),
            "backend_mb": round(rss / 2**20),
            "ollama_mb": loaded_models_mb(self.ollama_host),
            "system_mem_pct": round(vm.percent, 1),
            "t": time.time(),
        }
