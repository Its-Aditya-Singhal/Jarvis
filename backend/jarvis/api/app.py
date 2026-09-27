"""FastAPI app: local REST + WebSocket API for the desktop shell.

Binds to 127.0.0.1 only. When ``JARVIS_API_TOKEN`` is set (always, when
launched by the desktop app) every request must carry it.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from ..audio.mic import Microphone
from ..auth.face.engine import FaceEngine
from ..auth.levels import REASONS
from ..auth.voice.engine import SpeakerEngine
from ..camera.capture import Camera
from ..config import Settings, get_settings
from ..database.db import Database
from ..events import EventBus
from ..security.crypto import KeychainKeyProvider, KeyProvider
from ..security.template_store import TemplateStore
from ..brain import Brain
from ..tools.apple import AppleBridge, AppleError
from ..tools.apps import AppIndex
from ..tools.files import FileSearch, FolderError
from ..tools.runner import ToolRunner
from ..tools.scheduler import AlarmScheduler
from ..tools.store import ToolStore
from ..service import AssistantService
from ..speech.stt import SpeechToText
from ..speech.tts import TextToSpeech
from ..speech_service import SpeechService
from ..voice_service import VoiceService

log = logging.getLogger(__name__)


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


class PathIn(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class AppleIn(BaseModel):
    calendar_sync: bool
    calendar: str = Field(default="", max_length=200)
    notes_sync: bool


class ConfirmIn(BaseModel):
    accept: bool


class SnoozeIn(BaseModel):
    minutes: int = Field(default=5, ge=1, le=60)


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
            mic or Microphone(s.mic_device),
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
        runner = ToolRunner(db, tstore, apps or AppIndex(), files, apple_bridge, on_change=svc.tools_changed)
        return runner, AlarmScheduler(tstore, on_ring=svc.ring, on_change=svc.tools_changed)

    def make_brain(svc: AssistantService) -> Brain:
        return brain or Brain(s, db, names=lambda: (svc.assistant_name, svc.owner_name), voice_gender=lambda: db.get("voice_gender", "female"))

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
        allow_origins=["http://localhost:1420", "tauri://localhost", "http://tauri.localhost"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
        # Enrollment/profile changes are open only during first-time setup.
        # Re-enrollment by a verified owner arrives with the privacy dashboard (phase 9).
        if svc.setup_complete:
            raise HTTPException(409, "setup already completed")

    auth = [Depends(require_token)]

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

    @app.post("/api/enroll/face/start", dependencies=auth + [Depends(require_setup_open)])
    def enroll_start():
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
    def set_llm(body: ModelIn):
        if svc.brain is None:
            raise HTTPException(503, "language model disabled")
        try:
            svc.brain.set_model(body.model)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        bus.log(f"Language model set to {body.model}")
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

    @app.post("/api/alarms/{alarm_id}/cancel", dependencies=auth + [Depends(require_owner)])
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
        return {"folders": [str(p) for p in files.folders()]}

    @app.put("/api/settings/files", dependencies=auth + [Depends(require_level2)])
    def set_folders(body: FoldersIn):
        try:
            saved = files.set_folders(body.folders)
        except FolderError as exc:
            raise HTTPException(400, str(exc)) from exc
        bus.log(f"File search folders updated ({len(saved)})")
        return {"folders": [str(p) for p in saved]}

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

    @app.post("/api/apple/sync", dependencies=auth + [Depends(require_owner)])
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
