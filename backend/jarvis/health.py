"""Turns component status into plain problems with a fix.

Each issue: ``{"id", "level" (error|warn), "title", "fix"}``. The UI shows
them on the System view; errors are also spoken once after startup.
"""

from __future__ import annotations

import shutil
from pathlib import Path

DOWNLOAD = "Open Settings → Models and press Download, then Restart."
READY = {"ready", "disabled"}


def _model_issue(key: str, st: str, title: str) -> dict | None:
    if not st or st in READY or st == "loading" or st.startswith("challenges only"):
        return None
    missing = "missing" in st or "not found" in st or "No such file" in st
    return {"id": f"model_{key}", "level": "error", "title": f"{title}: {st}",
            "fix": DOWNLOAD if missing else "Restart the app; if it persists, re-download the models. " + DOWNLOAD}


def mic_fix(cause: str | None, app: str) -> str:
    settings = f"System Settings → Privacy & Security → Microphone → turn on {app}"
    fixes = {
        "permission": f"{settings}, then quit and reopen {app}. You can still type commands.",
        "silent": "Pick your real microphone in Settings → Microphone and check its input level in System "
                  f"Settings → Sound → Input. If that doesn't help: {settings} (if it's already on, turn it off "
                  f"and on again), then quit and reopen {app}.",
        "no_device": "Connect a microphone, or check System Settings → Sound → Input, then press Retry "
                     "in Settings → Microphone.",
        "device_missing": "Reconnect the chosen microphone or pick another one in Settings → Microphone.",
    }
    return fixes.get(cause or "", "Close other apps that may be using the microphone and press Retry in "
                     f"Settings → Microphone. If it keeps failing: {settings}.")


def issues(status: dict, data_dir: Path | None = None, perf: dict | None = None) -> list[dict]:
    out: list[dict] = []
    app = status.get("app") or "JARVIS"
    cam = status.get("camera") or {}
    if cam.get("status") == "error":
        out.append({"id": "camera", "level": "error", "title": "Camera unavailable — I can't verify you",
                    "fix": f"Allow camera access for {app} in System Settings → Privacy & Security → Camera, "
                           "close other apps using the camera, then restart."})
    mic = status.get("mic") or {}
    if mic.get("status") == "error":
        out.append({"id": "mic", "level": "error",
                    "title": f"{mic.get('error') or 'Microphone unavailable'} — voice commands are off",
                    "fix": mic_fix(mic.get("cause"), app)})
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
        elif "gemini" in llm.lower():
            out.append({"id": "llm", "level": "error", "title": "AI not reachable — I can only do instant commands",
                        "fix": llm[:1].upper() + llm[1:] + "." if "Settings" in llm else
                        "Add your free Gemini API key in Settings → AI, and check the internet connection."})
        elif "bedrock" in llm.lower():
            out.append({"id": "llm", "level": "error", "title": "AI not reachable — I can only do instant commands",
                        "fix": llm[:1].upper() + llm[1:] + "." if "Settings" in llm else
                        "Add your Bedrock API key in Settings → AI, or switch back to Gemini."})
        else:
            out.append({"id": "llm", "level": "error", "title": "Local AI offline — I can only do instant commands",
                        "fix": "Start Ollama from Settings → Models (or install it from ollama.com/download)."})
    mem = models.get("memory", "")
    if mem.startswith("word match only") and "off (" not in mem:
        out.append({"id": "memory", "level": "warn", "title": "Memory recall by word match only",
                    "fix": "Run: ollama pull bge-m3 — then restart for meaning-based recall."})
    denied = status.get("files_denied") or []
    if denied:
        out.append({"id": "files", "level": "warn",
                    "title": f"No access to {', '.join(denied)} — file search can't look there",
                    "fix": "System Settings → Privacy & Security → Files and Folders → turn on "
                           f"{', '.join(denied)} for {app}, then restart the app."})
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
