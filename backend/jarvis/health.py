"""Turns component status into plain problems with a fix.

Each issue: ``{"id", "level" (error|warn), "title", "fix"}``. The UI shows
them on the System view; errors are also spoken once after startup.
"""

from __future__ import annotations

import shutil
from pathlib import Path

DOWNLOAD = "Run: backend/.venv/bin/python scripts/download_models.py"
READY = {"ready", "disabled"}


def _model_issue(key: str, st: str, title: str) -> dict | None:
    if not st or st in READY or st == "loading" or st.startswith("challenges only"):
        return None
    missing = "missing" in st or "not found" in st or "No such file" in st
    return {"id": f"model_{key}", "level": "error", "title": f"{title}: {st}",
            "fix": DOWNLOAD if missing else "Restart the app; if it persists, re-download the models. " + DOWNLOAD}


def issues(status: dict, data_dir: Path | None = None, perf: dict | None = None) -> list[dict]:
    out: list[dict] = []
    cam = status.get("camera") or {}
    if cam.get("status") == "error":
        out.append({"id": "camera", "level": "error", "title": "Camera unavailable — I can't verify you",
                    "fix": "Allow camera access in System Settings → Privacy & Security → Camera (for JARVIS, or "
                           "Terminal while developing), close other apps using the camera, then restart."})
    mic = status.get("mic") or {}
    if mic.get("status") == "error":
        out.append({"id": "mic", "level": "error", "title": "Microphone unavailable — voice commands are off",
                    "fix": "Allow microphone access in System Settings → Privacy & Security → Microphone, then "
                           "restart. You can still type commands."})
    models = status.get("models") or {}
    for key, title in (("face", "Face recognition"), ("voice", "Speaker recognition"),
                       ("stt", "Speech recognition"), ("tts", "Voice synthesis")):
        if (i := _model_issue(key, models.get(key, ""), title)):
            out.append(i)
    live = models.get("liveness", "")
    if live.startswith("challenges only"):
        out.append({"id": "model_liveness", "level": "warn", "title": "Anti-spoof model missing — using challenges only",
                    "fix": DOWNLOAD})
    llm = models.get("llm", "")
    if llm and llm not in READY and llm != "loading":
        if "not installed" in llm:
            fix = llm.split("run: ", 1)[-1] if "run: " in llm else "ollama pull <model>"
            out.append({"id": "llm", "level": "error", "title": "Language model not installed", "fix": f"Run: {fix}"})
        else:
            out.append({"id": "llm", "level": "error", "title": "Local AI offline — I can only do instant commands",
                        "fix": "Start Ollama: ~/Developer/ollama/start.sh (or install it: brew install ollama)."})
    mem = models.get("memory", "")
    if mem.startswith("word match only"):
        out.append({"id": "memory", "level": "warn", "title": "Memory recall by word match only",
                    "fix": "Run: ollama pull bge-m3 — then restart for meaning-based recall."})
    if data_dir is not None:
        try:
            free = shutil.disk_usage(data_dir if data_dir.exists() else data_dir.parent).free
            if free < 1 << 30:
                out.append({"id": "disk", "level": "warn", "title": f"Low disk space ({free >> 20} MB free)",
                            "fix": "Free some space — history and notes may fail to save."})
        except OSError:
            pass
    if perf and perf.get("system_mem_pct", 0) >= 92:
        out.append({"id": "memory_pressure", "level": "warn", "title": "The Mac is low on memory",
                    "fix": "Switch to Fast mode in Settings → Performance, or close other apps."})
    return out
