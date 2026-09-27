"""FastAPI app: local REST + WebSocket API for the desktop shell.

Binds to 127.0.0.1 only. When ``JARVIS_API_TOKEN`` is set (always, when
launched by the desktop app) every request must carry it.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from .. import health
from ..audio import mic as mic_mod
from ..audio.mic import Microphone
from ..auth.face.engine import FaceEngine
from ..auth.levels import REASONS
from ..auth.voice.engine import SpeakerEngine
from ..brain import Brain
from ..camera.capture import Camera
from ..config import Settings, get_settings
from ..database.db import Database
from ..downloads import DownloadError, ModelDownloader
from ..events import EventBus
from ..host import responsible_app
from ..llm.client import OllamaClient
from ..llm.server import OllamaServer
from ..llm.setup import OllamaSetup
from ..memory.manager import RETENTION_CHOICES, Memory
from ..memory.store import MemoryStore
from ..netguard import NetGuard
from ..perf import PerfMonitor
from ..prefs import PREFS, coerce
from ..privacy import ACTIONS as PRIVACY_ACTIONS
from ..privacy import inventory
from ..security.crypto import KeychainKeyProvider, KeyProvider
from ..security.template_store import TemplateStore
from ..service import AssistantService
from ..speech.stt import SpeechToText
from ..speech.tts import TextToSpeech
from ..speech_service import SpeechService
from ..tools.apple import AppleBridge, AppleError
from ..tools.apps import AppIndex
from ..tools.files import FileSearch, FolderError
from ..tools.mac import MacControl
from ..tools.runner import ToolRunner
from ..tools.scheduler import AlarmScheduler
from ..tools.store import ToolStore
from ..voice_service import VoiceService

log = logging.getLogger(__name__)

# the desktop shell's web view, and the Vite dev server
APP_ORIGINS = ["http://localhost:1420", "tauri://localhost", "http://tauri.localhost"]
# the backend only ever serves this machine: any other Host header is a DNS-rebinding attempt
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _host_name(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):  # [::1]:8765
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class LocalOnly:
    """Refuse requests whose Host isn't this machine, and WebSockets opened by other web pages
    (browsers send Origin on WebSockets, and CORS doesn't apply to them)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
            host_ok = _host_name(headers.get("host", "")) in LOCAL_HOSTS
            origin = headers.get("origin")
            origin_ok = scope["type"] == "http" or origin is None or origin in APP_ORIGINS
            if not (host_ok and origin_ok):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4403})
                else:
                    await send({"type": "http.response.start", "status": 400,
                                "headers": [(b"content-type", b"text/plain")]})
                    await send({"type": "http.response.body", "body": b"not a local request"})
                return
        await self.app(scope, receive, send)


Gender = Literal["female", "male"]


class ProfileIn(BaseModel):
    owner_name: str = Field(min_length=1, max_length=40)
    assistant_name: str = Field(min_length=1, max_length=24)
    voice_gender: Gender = "female"

    @field_validator("owner_name", "assistant_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("must not be blank")
        return v


class VoiceIn(BaseModel):
    gender: Gender


class CommandIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ModelIn(BaseModel):
    model: str = Field(min_length=1, max_length=100)


class FoldersIn(BaseModel):
    folders: list[str] = Field(max_length=30)


class MicIn(BaseModel):
    device: str | None = Field(default=None, max_length=200)  # None: the system default


class PathIn(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class AppleIn(BaseModel):
    calendar_sync: bool
    calendar: str = Field(default="", max_length=200)
    notes_sync: bool


class ConfirmIn(BaseModel):
    accept: bool


class FactIn(BaseModel):
    text: str = Field(min_length=1, max_length=300)


class RetentionIn(BaseModel):
    retention: Literal["off", "7d", "30d", "forever"]


class PrefIn(BaseModel):
    key: str = Field(min_length=1, max_length=40)
    value: bool | float | str


class NamesIn(BaseModel):
    owner_name: str = Field(min_length=1, max_length=40)
    assistant_name: str = Field(min_length=1, max_length=24)

    @field_validator("owner_name", "assistant_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return ProfileIn._strip(v)


class LlmSlotIn(BaseModel):
    model: str = Field(min_length=1, max_length=100)
    slot: Literal["main", "fast"] = "main"


class PrivacyIn(BaseModel):
    path: str | None = Field(default=None, max_length=1024)


class SnoozeIn(BaseModel):
    minutes: int = Field(default=5, ge=1, le=60)


class PacksIn(BaseModel):
    packs: list[str] | None = Field(default=None, max_length=20)  # None: the required ones still missing


def restart_process() -> None:
    """Start this backend again in the same process (same port, token and parent), so it
    loads models downloaded after it started. The UI reconnects on its own."""
    logging.shutdown()
    if getattr(sys, "frozen", False):
        os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
    args = list(getattr(sys, "orig_argv", [])) or [sys.executable, *sys.argv]
    os.execv(sys.executable, [sys.executable, *args[1:]])


def create_app(
    settings: Settings | None = None,
    keys: KeyProvider | None = None,
    engine: FaceEngine | None = None,
    camera: Camera | None = None,
    speaker_engine: SpeakerEngine | None = None,
    mic: Microphone | None = None,
    vad_factory=None,
    voice: bool = True,
    stt: SpeechToText | None = None,
    tts: TextToSpeech | None = None,
    player=None,
    speech: bool = True,
    brain: Brain | None = None,
    llm: bool = True,
    apple: AppleBridge | None = None,
    apps: AppIndex | None = None,
    tools: bool = True,
    memory: bool = True,
    memory_client: OllamaClient | None = None,
    guard: NetGuard | None = None,
    perf: PerfMonitor | None = None,
    mac: MacControl | None = None,
    llm_client: OllamaClient | None = None,
    llm_server: OllamaServer | None = None,
    downloader: ModelDownloader | None = None,
    restart: Callable[[], None] = restart_process,
) -> FastAPI:
    s = settings or get_settings()
    db = Database(s.db_path)
    keyp = keys or KeychainKeyProvider(s.keychain_service)
    store = TemplateStore(s.templates_dir, keyp)
    bus = EventBus()

    def make_voice(svc: AssistantService) -> VoiceService:
        extra = {"vad_factory": vad_factory} if vad_factory else {}
        return VoiceService(
            s,
            db,
            store,
            bus,
            speaker_engine or SpeakerEngine(s.models_dir),
            mic or Microphone(db.get("audio.mic_device") or s.mic_device),
            owner_verified=svc.owner_verified,
            names=lambda: (svc.assistant_name, svc.owner_name),
            speech=svc.speech,
            **extra,
        )

    def make_speech(svc: AssistantService) -> SpeechService:
        return SpeechService(
            s,
            db,
            bus,
            stt or SpeechToText(s.models_dir, s.stt_model),
            tts or TextToSpeech(s.models_dir, speed=s.tts_speed),
            owner_verified=svc.owner_verified,
            names=lambda: (svc.assistant_name, svc.owner_name),
            player=player,
            on_command=lambda text, lang: svc.command(text, lang, "voice"),
        )

    files = FileSearch(db, protected=[s.data_dir])
    apple_bridge = apple or AppleBridge()

    def make_tools(svc: AssistantService) -> tuple[ToolRunner, AlarmScheduler]:
        tstore = ToolStore(db, keyp)
        runner = ToolRunner(db, tstore, apps or AppIndex(), files, apple_bridge, on_change=svc.tools_changed, mac=mac)
        return runner, AlarmScheduler(tstore, on_ring=svc.ring, on_change=svc.tools_changed)

    def make_brain(svc: AssistantService) -> Brain:
        return brain or Brain(s, db, names=lambda: (svc.assistant_name, svc.owner_name),
                              voice_gender=lambda: db.get("voice_gender") or "female", server=llm_server, client=llm_client)

    def make_memory(svc: AssistantService) -> Memory:
        client = memory_client or (svc.brain.client if svc.brain else OllamaClient(f"http://{s.ollama_host}", 30.0))
        return Memory(
            db, MemoryStore(db, keyp), client, s.embed_model,
            extractor=svc.brain.extract_facts if svc.brain else None,
            on_change=svc.memory_changed,
        )

    svc = AssistantService(
        s,
        db,
        store,
        bus,
        engine or FaceEngine(s.models_dir, s.face_model_pack),
        camera or Camera(s.camera_index),
        voice_factory=make_voice if voice else None,
        speech_factory=make_speech if (speech and s.speech_enabled) else None,
        brain_factory=make_brain if llm else None,
        tools_factory=make_tools if tools else None,
        memory_factory=make_memory if (memory and s.memory_enabled) else None,
        guard=guard,
        perf=perf,
    )

    models = downloader or ModelDownloader(s.models_dir, guard=svc.guard)
    svc.downloader = models
    started_without_models = bool(models.needed())  # this process loaded none of them

    def llm_wanted() -> list[dict]:
        main = db.get("llm_model") or s.llm_model
        out = [{"name": main, "purpose": "Understands requests and answers questions", "required": True}]
        if s.memory_enabled:
            out.append({"name": s.embed_model, "purpose": "Memory recall by meaning", "required": False})
        fast = db.get("llm_fast_model") or "qwen2.5:3b"
        if fast != main:
            out.append({"name": fast, "purpose": "Fast mode (on battery)", "required": False})
        return out

    brain_ = svc.brain
    ollama = OllamaSetup(
        brain_.server if brain_ is not None else (llm_server or OllamaServer(s.ollama_host, s.ollama_models_dir, s.data_dir / "logs")),
        brain_.client if brain_ is not None else (llm_client or OllamaClient(f"http://{s.ollama_host}", s.llm_timeout_s)),
        llm_wanted,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus.bind(asyncio.get_running_loop())
        # model loading is slow; serve requests (and report "loading") meanwhile
        threading.Thread(target=svc.start, name="startup", daemon=True).start()
        yield
        await asyncio.to_thread(svc.stop)
        db.close()

    app = FastAPI(title="Assistant backend", lifespan=lifespan)
    app.state.svc = svc
    app.add_middleware(
        CORSMiddleware,
        allow_origins=APP_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_middleware(LocalOnly)

    def token_ok(token: str | None) -> bool:
        if not s.api_token:
            return True
        return token is not None and hmac.compare_digest(token, s.api_token)

    def require_token(authorization: str | None = Header(default=None)) -> None:
        token = authorization.removeprefix("Bearer ") if authorization else None
        if not token_ok(token):
            raise HTTPException(401, "invalid token")

    def require_owner() -> None:
        """Level 1: the live, verified owner is at the screen."""
        if not svc.owner_verified():
            raise HTTPException(403, "owner verification required")

    def require_level2() -> None:
        """Level 2 for changes that widen access or touch security."""
        t = svc.trust()
        if t.level < 1:
            raise HTTPException(403, "owner verification required")
        if t.level < 2:
            code = t.blockers.get(2, "")
            hint = " — talk to me first (say my name and anything), then try again" if code == "voice_needed" else ""
            raise HTTPException(403, f"needs level 2: {REASONS.get(code, code)}{hint}")

    def require_setup_open() -> None:
        # the profile is created only during first-time setup; later changes go
        # through Settings (level 2) and face re-scans through a confirmation
        if svc.setup_complete:
            raise HTTPException(409, "setup already completed")

    def require_models_access() -> None:
        """Model downloads: open during first-run setup and whenever a required model is
        missing (nobody can be verified without them); otherwise the owner only."""
        if svc.setup_complete and not models.needed():
            require_owner()

    def require_restart_access() -> None:
        """A backend that started before its models arrived may always reload them:
        until it does, nobody can be verified."""
        if not started_without_models:
            require_models_access()

    auth = [Depends(require_token)]
    models_auth = auth + [Depends(require_models_access)]

    @app.get("/api/models", dependencies=auth)
    def models_state():
        return {"files": models.status(), "ollama": ollama.detect()}

    @app.post("/api/models/download", dependencies=models_auth)
    def models_download(body: PacksIn | None = None):
        try:
            return models.start(body.packs if body else None)
        except DownloadError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/models/cancel", dependencies=models_auth)
    def models_cancel():
        models.cancel()
        return {"ok": True}

    @app.post("/api/models/ollama/start", dependencies=models_auth)
    def ollama_start():
        return ollama.start_server()

    @app.post("/api/models/ollama/pull", dependencies=models_auth)
    def ollama_pull(body: ModelIn):
        try:
            return ollama.pull(body.model)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/models/restart", dependencies=auth + [Depends(require_restart_access)])
    def models_restart():
        """Reload with the new models: the backend restarts in place a moment after answering."""
        if models.running:
            raise HTTPException(409, "the download is still running")

        def later() -> None:
            time.sleep(0.3)
            try:
                svc.stop()  # releases the camera and microphone first
            finally:
                restart()

        threading.Thread(target=later, name="restart", daemon=True).start()
        return {"ok": True}

    @app.get("/api/status", dependencies=auth)
    def status():
        return svc.status()

    @app.post("/api/setup/profile", dependencies=auth + [Depends(require_setup_open)])
    def set_profile(body: ProfileIn):
        db.set("owner_name", body.owner_name)
        db.set("assistant_name", body.assistant_name)
        db.set("voice_gender", body.voice_gender)
        bus.log(f"Profile created — assistant named {body.assistant_name}")
        return svc.status()

    @app.post("/api/enroll/face/start", dependencies=auth)
    def enroll_start():
        allowed, why = svc.face_enroll_allowed()
        if not allowed:
            raise HTTPException(403, why)
        if not svc.engine.ready:
            raise HTTPException(503, svc.engine.error or "face model not ready")
        if svc.camera.status != "active":
            raise HTTPException(503, svc.camera.error or "camera not active")
        svc.begin_enrollment()
        return {"ok": True}

    @app.post("/api/enroll/face/cancel", dependencies=auth)
    def enroll_cancel():
        svc.cancel_enrollment()
        return {"ok": True}

    @app.post("/api/enroll/face/keep", dependencies=auth)
    def enroll_keep():
        svc.end_rescan()  # only ever keeps the existing profile
        return {"ok": True}

    def voice_or_503() -> VoiceService:
        v = svc.voice
        if v is None or not v.ready:
            raise HTTPException(503, (v.model_status() if v else "voice pipeline disabled"))
        if v.mic.status != "active":
            raise HTTPException(503, v.mic.error or "microphone not active")
        return v

    @app.post("/api/enroll/voice/start", dependencies=auth)
    def voice_enroll_start():
        # during setup anyone at the keyboard is the owner-to-be; afterwards
        # (re-enrollment) only the face-verified owner may replace the voice profile
        if svc.setup_complete and not svc.owner_verified():
            raise HTTPException(403, "owner verification required")
        if not svc.owner_name:
            raise HTTPException(400, "create a profile first")
        voice_or_503().begin_enrollment(needs_owner=svc.setup_complete)
        return {"ok": True}

    @app.post("/api/enroll/voice/cancel", dependencies=auth)
    def voice_enroll_cancel():
        if svc.voice is not None:
            svc.voice.cancel_enrollment()
        return {"ok": True}

    @app.post("/api/setup/complete", dependencies=auth + [Depends(require_setup_open)])
    def setup_complete():
        if not (svc.owner_name and svc.face_enrolled):
            raise HTTPException(400, "profile and face enrollment required")
        # voice is required whenever the voice system works on this machine
        if svc.voice is not None and svc.voice.available and not svc.voice_enrolled:
            raise HTTPException(400, "voice enrollment required")
        db.set("setup_complete", "1")
        svc.begin_verification()
        bus.log("Setup complete — identity profile active", "ok")
        return svc.status()

    def speech_or_503() -> SpeechService:
        sp = svc.speech
        if sp is None or not sp.tts.ready:
            raise HTTPException(503, sp.tts.error or "voice synthesis loading" if sp else "speech disabled")
        return sp

    @app.post("/api/speech/preview", dependencies=auth)
    def speech_preview(body: VoiceIn):
        # during setup anyone at the keyboard is the owner-to-be
        if svc.setup_complete and not svc.owner_verified():
            raise HTTPException(403, "owner verification required")
        speech_or_503().preview(body.gender)
        return {"ok": True}

    @app.post("/api/speech/stop", dependencies=auth)
    def speech_stop():
        if svc.speech is not None:
            svc.speech.out.interrupt()
        return {"ok": True}

    @app.put("/api/settings/voice", dependencies=auth + [Depends(require_owner)])
    def set_voice(body: VoiceIn):
        db.set("voice_gender", body.gender)
        bus.log(f"Assistant voice set to {body.gender}")
        return svc.status()

    @app.post("/api/command", dependencies=auth + [Depends(require_owner)])
    def typed_command(body: CommandIn):
        from ..speech.text import has_devanagari

        return svc.command(body.text, "hi" if has_devanagari(body.text) else "en", "typed")

    @app.get("/api/llm/models", dependencies=auth + [Depends(require_owner)])
    def llm_models():
        if svc.brain is None:
            raise HTTPException(503, "language model disabled")
        return {"current": svc.brain.model, "installed": svc.brain.installed_models(), "status": svc.brain.status()}

    @app.put("/api/settings/llm", dependencies=auth + [Depends(require_owner)])
    def set_llm(body: LlmSlotIn):
        if svc.brain is None:
            raise HTTPException(503, "language model disabled")
        try:
            if body.slot == "fast":
                if body.model not in svc.brain.installed_models():
                    raise ValueError(f"model {body.model} is not installed")
                db.set("llm_fast_model", body.model)
            else:
                svc.brain.set_model(body.model)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        bus.log(f"{'Fast-mode' if body.slot == 'fast' else 'Language'} model set to {body.model}")
        svc.apply_mode()
        threading.Thread(target=svc.brain.start, daemon=True).start()  # warm the new model
        return svc.status()

    # -- tools -------------------------------------------------------------------------
    def tools_or_503() -> ToolRunner:
        if svc.tools is None:
            raise HTTPException(503, "tools disabled")
        return svc.tools

    @app.get("/api/tools", dependencies=auth + [Depends(require_owner)])
    def tools_state():
        t = tools_or_503()
        now = datetime.now()
        from datetime import timedelta

        events = t.store.events_between(now.replace(hour=0, minute=0), now + timedelta(days=14))
        return {
            "alarms": [
                {"id": a.id, "kind": a.kind, "due": a.due.isoformat(timespec="seconds"), "label": a.label, "status": a.status}
                for a in t.store.alarms()
            ],
            "events": [
                {"id": e.id, "title": e.title, "start": e.start.isoformat(timespec="minutes"),
                 "end": e.end.isoformat(timespec="minutes"), "apple": e.apple_uid is not None}
                for e in events
            ],
            "notes": [
                {"id": n.id, "text": n.text, "created": n.created.isoformat(timespec="minutes"), "apple": n.apple_id is not None}
                for n in t.store.notes(30)
            ],
        }

    # the same level as the alarm.cancel tool (tools.runner.LEVELS)
    @app.post("/api/alarms/{alarm_id}/cancel", dependencies=auth + [Depends(require_level2)])
    def alarm_cancel(alarm_id: int):
        if svc.alarms is None:
            raise HTTPException(503, "tools disabled")
        svc.alarms.cancel(alarm_id)
        bus.log("Alarm cancelled")
        return {"ok": True}

    # silencing a ringing alarm needs no verification, like a phone alarm
    @app.post("/api/alarms/dismiss", dependencies=auth)
    def alarm_dismiss():
        return {"dismissed": svc.dismiss_alarms()}

    @app.post("/api/alarms/snooze", dependencies=auth)
    def alarm_snooze(body: SnoozeIn):
        return {"snoozed": svc.snooze_alarms(body.minutes)}

    @app.get("/api/settings/files", dependencies=auth + [Depends(require_owner)])
    def get_folders():
        return {"folders": [str(p) for p in files.folders()], "status": files.status()}

    @app.get("/api/mic", dependencies=auth)
    def get_mic():
        info = svc.mic_info()
        return {**info, "devices": mic_mod.input_devices(), "permission": mic_mod.permission(),
                "app": responsible_app(),
                "fix": health.mic_fix(info.get("cause"), responsible_app()) if info["status"] == "error" else None}

    @app.post("/api/mic/retry", dependencies=auth)
    def retry_mic():
        if svc.voice is None:
            raise HTTPException(503, "voice pipeline disabled")
        if hasattr(svc.voice.mic, "retry"):
            svc.voice.mic.retry()
        return {"ok": True}

    # a different input (e.g. a virtual audio device) could feed recordings to
    # voice verification, so choosing one is a security change
    @app.put("/api/mic", dependencies=auth + [Depends(require_level2)])
    def set_mic(body: MicIn):
        if svc.voice is None:
            raise HTTPException(503, "voice pipeline disabled")
        name = (body.device or "").strip() or None
        if name is not None and name not in {d["name"] for d in mic_mod.input_devices()}:
            raise HTTPException(400, f"no microphone called {name!r} is connected")
        db.set("audio.mic_device", name or "")
        db.add_security_event("settings_changed", f"microphone set to {name or 'system default'}")
        if hasattr(svc.voice.mic, "use"):
            svc.voice.mic.use(name)
        bus.log(f"Microphone: {name or 'system default'}")
        return {"ok": True}

    @app.put("/api/settings/files", dependencies=auth + [Depends(require_level2)])
    def set_folders(body: FoldersIn):
        try:
            saved = files.set_folders(body.folders)
        except FolderError as exc:
            raise HTTPException(400, str(exc)) from exc
        bus.log(f"File search folders updated ({len(saved)})")
        return {"folders": [str(p) for p in saved], "status": files.status()}

    @app.post("/api/files/reveal", dependencies=auth + [Depends(require_owner)])
    def reveal(body: PathIn):
        try:
            files.reveal(body.path)
        except FolderError as exc:
            raise HTTPException(403, str(exc)) from exc
        return {"ok": True}

    def apple_settings() -> dict:
        return {
            "calendar_sync": db.get("apple_calendar_sync") == "1",
            "calendar": db.get("apple_calendar_name") or "",
            "notes_sync": db.get("apple_notes_sync") == "1",
            "notes_folder": "JARVIS",
        }

    @app.get("/api/settings/apple", dependencies=auth + [Depends(require_owner)])
    def get_apple():
        return apple_settings()

    @app.get("/api/apple/calendars", dependencies=auth + [Depends(require_owner)])
    def apple_calendars():
        # first use triggers macOS's "control Calendar" permission prompt
        try:
            return {"calendars": apple_bridge.calendars()}
        except AppleError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.put("/api/settings/apple", dependencies=auth + [Depends(require_level2)])
    def set_apple(body: AppleIn):
        if body.calendar_sync and not body.calendar:
            raise HTTPException(400, "choose an Apple calendar to sync with")
        db.set("apple_calendar_sync", "1" if body.calendar_sync else "0")
        db.set("apple_calendar_name", body.calendar)
        db.set("apple_notes_sync", "1" if body.notes_sync else "0")
        bus.log("Apple sync settings updated")
        return apple_settings()

    # creates items in Calendar / Notes, like calendar.create and notes.add (level 2)
    @app.post("/api/apple/sync", dependencies=auth + [Depends(require_level2)])
    def apple_sync_now():
        try:
            pushed = tools_or_503().sync_now()
        except AppleError as exc:
            raise HTTPException(502, str(exc)) from exc
        bus.log(f"Synced to Apple: {pushed['events']} events, {pushed['notes']} notes", "ok")
        return pushed

    # -- auth levels & fusion -------------------------------------------------------------
    @app.post("/api/confirm/{pid}", dependencies=auth + [Depends(require_owner)])
    def confirm(pid: str, body: ConfirmIn):
        # the level-3 checks (level 2, fresh liveness, fusion) happen inside
        return svc.confirm(pid, body.accept, "click")

    @app.get("/api/fusion", dependencies=auth + [Depends(require_owner)])
    def fusion_info():
        return svc.fusion_info()

    @app.post("/api/fusion/retrain", dependencies=auth + [Depends(require_level2)])
    def fusion_retrain():
        try:
            return svc.retrain_fusion()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/fusion/reset", dependencies=auth + [Depends(require_level2)])
    def fusion_reset():
        return svc.reset_fusion()

    # -- memory ------------------------------------------------------------------------------
    def memory_or_503() -> Memory:
        if svc.memory is None:
            raise HTTPException(503, "memory disabled")
        return svc.memory

    def fact_out(f) -> dict:
        return {"id": f.id, "text": f.text, "source": f.source,
                "created": f.created.isoformat(timespec="minutes"), "updated": f.updated.isoformat(timespec="minutes")}

    @app.get("/api/memory", dependencies=auth + [Depends(require_owner)])
    def memory_state():
        m = memory_or_503()
        return {
            "facts": [fact_out(f) for f in m.facts()],
            "suggestions": [{"id": x.id, "text": x.text} for x in m.suggestions()],
            "status": m.status(),
            "retention_choices": list(RETENTION_CHOICES),
        }

    @app.post("/api/memory", dependencies=auth + [Depends(require_level2)])
    def memory_add(body: FactIn):
        fact, _ = memory_or_503().remember(body.text, "typed")
        if fact is None:
            raise HTTPException(400, "nothing to remember")
        return fact_out(fact)

    @app.put("/api/memory/{fact_id}", dependencies=auth + [Depends(require_level2)])
    def memory_edit(fact_id: int, body: FactIn):
        if not memory_or_503().update(fact_id, body.text):
            raise HTTPException(404, "no such memory")
        return {"ok": True}

    @app.delete("/api/memory/{fact_id}", dependencies=auth + [Depends(require_level2)])
    def memory_delete(fact_id: int):
        if not memory_or_503().delete(fact_id):
            raise HTTPException(404, "no such memory")
        bus.log("Memory deleted")
        return {"ok": True}

    @app.post("/api/memory/suggestions/{sid}", dependencies=auth + [Depends(require_owner)])
    def memory_suggestion(sid: str, body: ConfirmIn):
        memory_or_503()
        if body.accept:
            require_level2()  # saving needs the owner's voice, dismissing doesn't
        return svc.accept_suggestion(sid, body.accept)

    @app.get("/api/history", dependencies=auth + [Depends(require_owner)])
    def history(days: int = Query(default=30, ge=1, le=3650), q: str = Query(default="", max_length=200)):
        m = memory_or_503()
        if q.strip():
            return {"turns": [
                {"time": y.ts.isoformat(timespec="seconds"), "you": y.text, "reply": r.text if r else None}
                for y, r in m.search_history(q, k=20)
            ], "search": True}
        turns = sorted(m.history(days), key=lambda t: t.ts)
        pairs, pending = [], None
        for t in turns:
            if t.role == "you":
                if pending:
                    pairs.append(pending)
                pending = {"time": t.ts.isoformat(timespec="seconds"), "you": t.text, "reply": None}
            elif pending is not None and pending["reply"] is None:
                pending["reply"] = t.text
        if pending:
            pairs.append(pending)
        return {"turns": pairs[::-1], "search": False}

    @app.put("/api/settings/history", dependencies=auth + [Depends(require_level2)])
    def set_retention(body: RetentionIn):
        m = memory_or_503()
        m.set_retention(body.retention)
        bus.log(f"Conversation history retention: {body.retention}")
        return m.status()

    # -- settings & privacy --------------------------------------------------------------
    @app.get("/api/settings", dependencies=auth + [Depends(require_owner)])
    def settings_state():
        return {**svc.settings_info(), "owner_name": svc.owner_name, "assistant_name": svc.assistant_name,
                "voice_gender": db.get("voice_gender", "female")}

    @app.put("/api/settings/pref", dependencies=auth + [Depends(require_owner)])
    def set_pref(body: PrefIn):
        if body.key not in PREFS:
            raise HTTPException(404, "no such setting")
        try:
            loosens = svc.prefs.loosens(body.key, body.value)
            if PREFS[body.key].security:
                require_level2()  # tightening needs level 2; loosening a confirmation on top
            if loosens:
                r = svc.request_pref(body.key, coerce(PREFS[body.key], body.value))
                if not r["ok"]:
                    raise HTTPException(409, r["reply"])
                return {"pending": r["pending"], "reply": r["reply"]}
            return svc.set_pref(body.key, body.value)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/api/settings/profile", dependencies=auth + [Depends(require_level2)])
    def set_names(body: NamesIn):
        db.set("owner_name", body.owner_name)
        db.set("assistant_name", body.assistant_name)
        db.add_security_event("settings_changed", "Owner / assistant name changed")
        bus.log(f"Names updated — wake word is now “{body.assistant_name}”")
        if svc.brain is not None:
            svc.brain.clear()
            threading.Thread(target=svc.brain.prime, daemon=True).start()  # new names, new prompt prefix
        return svc.status()

    @app.get("/api/privacy", dependencies=auth + [Depends(require_owner)])
    def privacy_state():
        return {**inventory(svc), "network": svc.network_info()}

    @app.post("/api/recovery/reset", dependencies=auth)
    def recovery_reset():
        # only when no one can ever verify again (no face, no usable voice): the data
        # is erased, never revealed, so this gives nothing to whoever presses it
        if not svc.locked_out():
            raise HTTPException(403, "only available when no identity profile is left")
        svc._factory_reset()
        return svc.status()

    @app.post("/api/privacy/{action}", dependencies=auth + [Depends(require_level2)])
    def privacy_action(action: str, body: PrivacyIn | None = None):
        if action not in PRIVACY_ACTIONS:
            raise HTTPException(404, "no such action")
        if action in ("clear_memory", "clear_history") and svc.memory is None:
            raise HTTPException(503, "memory disabled")
        try:
            r = svc.request_privacy(action, path=body.path if body else None)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not r["ok"]:
            raise HTTPException(409, r["reply"])
        return r

    @app.get("/api/security/events", dependencies=auth + [Depends(require_owner)])
    def security_events(limit: int = 50):
        return [
            {
                "id": e["id"],
                "time": datetime.fromtimestamp(e["ts"]).isoformat(timespec="seconds"),
                "kind": e["kind"],
                "detail": e["detail"],
                "face_confidence": e["face_conf"],
                "voice_confidence": e["voice_conf"],
                "blocked": bool(e["blocked"]),
            }
            for e in db.security_events(limit)
        ]

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, token: str | None = Query(default=None)):
        if not token_ok(token):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        q = bus.subscribe()
        try:
            await websocket.send_json({"type": "status", **svc.status()})
            session = svc.voice.enrollment if svc.voice else None
            if session is not None:  # resume an in-progress enrollment after a UI reload
                await websocket.send_json({"type": "voice_enroll", **session.snapshot()})
            for entry in list(bus.activity):
                await websocket.send_json(entry)
            while True:
                await websocket.send_json(await q.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            bus.unsubscribe(q)

    return app
