"""What the assistant stores about the owner, and how to take it back.

``inventory`` lists every kind of stored data with its size and protection.
``export_data`` builds the readable export (never biometric templates).
The deletions themselves live on the service (they also reset live state)
and always go through a level-3 confirmation.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import AssistantService

ENC = "AES-256-GCM, key in the macOS Keychain"

# action -> (what the confirmation names, English question)
ACTIONS: dict[str, tuple[str, str]] = {
    "delete_face": ("your face profile", "Delete your face profile? You'll re-scan your face right after."),
    "reenroll_face": ("a new face scan", "Re-scan your face? Your current profile stays until the new one is saved."),
    "delete_voice": ("your voice profile", "Delete your voice profile?"),
    "clear_memory": ("everything I remember about you", "Forget everything I remember about you?"),
    "clear_history": ("all conversation history", "Delete all conversation history?"),
    "clear_tools": ("all notes, alarms and events", "Delete all notes, alarms, timers and events?"),
    "clear_security_log": ("the security log", "Clear the security log?"),
    "reset_fusion": ("the fusion model's learned samples", "Reset the fusion model and delete its device samples?"),
    "export": ("a readable copy of your data", "Export your data as a readable, unencrypted file?"),
    "factory_reset": ("everything", "Erase everything — profiles, memory, history, notes, settings and the "
                                    "encryption key — and start setup again?"),
}

NEVER_STORED = [
    "Camera images or video — frames are analysed in memory and dropped",
    "Audio recordings — utterances are analysed in memory and dropped",
    "Speech not addressed to the assistant — discarded without being shown or saved",
    "Anything sent to the internet — models run on this Mac and offline mode blocks network access",
]


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts).isoformat(timespec="minutes")


def _span(db, table: str, col: str) -> tuple[float | None, float | None]:
    r = db.run(f"SELECT MIN({col}) AS a, MAX({col}) AS b FROM {table}")[0]
    return r["a"], r["b"]


def inventory(svc: AssistantService) -> dict:
    db, store = svc.db, svc.store
    tables = set(db.tables())
    items = []

    for mod, title in (("face", "Face profile"), ("voice", "Voice profile")):
        info = store.info(mod)
        items.append({
            "id": mod, "title": title, "present": info is not None,
            "detail": (f"{info['samples']} embeddings · {info['bytes'] // 1024} KB" if info and info["samples"] is not None
                       else "stored, unreadable" if info else "not enrolled"),
            "updated": _iso(info["modified"]) if info else None,
            "protection": f"Embeddings only (no images/audio) · {ENC}",
            "action": f"delete_{mod}",
        })

    if "memories" in tables:
        n = db.count("memories")
        items.append({"id": "memory", "title": "Remembered facts", "present": n > 0, "detail": f"{n} facts",
                      "updated": _iso(_span(db, "memories", "updated")[1]), "protection": f"Text and embeddings · {ENC}",
                      "action": "clear_memory"})
    if "history" in tables:
        n = db.count("history")
        a, b = _span(db, "history", "ts")
        keep = svc.memory.retention if svc.memory else "—"
        items.append({"id": "history", "title": "Conversation history", "present": n > 0,
                      "detail": f"{n} messages · kept {keep}" + (f" · since {(_iso(a) or '')[:10]}" if a else ""),
                      "updated": _iso(b), "protection": f"Text only · {ENC}", "action": "clear_history"})
    if {"notes", "alarms", "events"} <= tables:
        c = {t: db.count(t) for t in ("notes", "alarms", "events")}
        items.append({"id": "tools", "title": "Notes, alarms & events", "present": any(c.values()),
                      "detail": f"{c['notes']} notes · {c['alarms']} alarms/timers · {c['events']} events",
                      "updated": None, "protection": f"Text · {ENC}", "action": "clear_tools"})
    samples = svc.samples.counts()
    items.append({"id": "fusion", "title": "Fusion model samples", "present": sum(samples.values()) > 0
                  or svc.fusion_source == "personal",
                  "detail": f"{samples['owner']} owner · {samples['other']} other · model: {svc.fusion_source}",
                  "updated": None, "protection": "Scores only (no identity data)", "action": "reset_fusion"})
    n = db.count("security_events")
    items.append({"id": "security", "title": "Security log", "present": n > 0, "detail": f"{n} events",
                  "updated": _iso(_span(db, "security_events", "ts")[1]),
                  "protection": "Times, outcomes and match percentages only", "action": "clear_security_log"})
    items.append({"id": "settings", "title": "Profile & settings", "present": True,
                  "detail": f"Names, voice, preferences ({db.count('profile')} entries)", "updated": None,
                  "protection": "Local database in your Library folder", "action": None})

    has_key = getattr(svc.store.keys, "has_key", None)
    return {
        "items": items,
        "never_stored": NEVER_STORED,
        "location": str(svc.s.data_dir),
        "keychain_key": has_key() if callable(has_key) else True,
        "db_bytes": svc.s.db_path.stat().st_size if svc.s.db_path.exists() else 0,
    }


def export_data(svc: AssistantService) -> dict:
    db = svc.db
    out: dict = {
        "exported": datetime.now().isoformat(timespec="seconds"),
        "note": "Readable copy of what the assistant stores. Face and voice templates are never exported.",
        "profile": {"owner_name": svc.owner_name, "assistant_name": svc.assistant_name,
                    "voice_gender": db.get("voice_gender", "female")},
        "preferences": {k: v for k, v in svc.prefs.all().items()},
    }
    m = svc.memory
    if m is not None:
        out["memory"] = [{"text": f.text, "source": f.source, "created": f.created.isoformat(timespec="minutes"),
                          "updated": f.updated.isoformat(timespec="minutes")} for f in m.facts()]
        out["history"] = [{"time": t.ts.isoformat(timespec="seconds"), "role": t.role, "text": t.text, "lang": t.lang}
                          for t in sorted(m.store.turns(), key=lambda t: t.ts)]
    t = svc.tools
    if t is not None:
        out["notes"] = [{"text": n.text, "created": n.created.isoformat(timespec="minutes")} for n in t.store.notes(10_000)]
        out["alarms"] = [{"kind": a.kind, "due": a.due.isoformat(timespec="minutes"), "label": a.label,
                          "status": a.status}
                         for a in t.store.alarms(("pending", "ringing", "done", "cancelled", "missed"))]
        out["events"] = [{"title": e.title, "start": e.start.isoformat(timespec="minutes"),
                          "end": e.end.isoformat(timespec="minutes")}
                         for e in t.store.events_between(datetime(1970, 1, 2), datetime(9999, 1, 1))]
    out["security_log"] = [{"time": _iso(e["ts"]), "kind": e["kind"], "detail": e["detail"], "blocked": bool(e["blocked"])}
                           for e in db.security_events(10_000)]
    out["fusion_samples"] = svc.samples.counts()
    return out


def validate_export_path(path: str, data_dir: Path) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValueError("choose a full path for the export")
    if p.suffix.lower() != ".json":
        p = p.with_suffix(".json")
    if not p.parent.is_dir():
        raise ValueError("that folder doesn't exist")
    try:
        p.resolve().relative_to(data_dir.resolve())
        raise ValueError("choose a folder outside the app's own data folder")
    except ValueError as exc:
        if "outside" in str(exc):
            raise
    return p


def write_export(svc: AssistantService, path: Path) -> int:
    data = json.dumps(export_data(svc), ensure_ascii=False, indent=2).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # readable by this user only
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return len(data)
