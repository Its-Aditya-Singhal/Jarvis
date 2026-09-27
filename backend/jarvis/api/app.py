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

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from ..audio.mic import Microphone
from ..auth.face.engine import FaceEngine
from ..auth.voice.engine import SpeakerEngine
from ..camera.capture import Camera
from ..config import Settings, get_settings
from ..database.db import Database
from ..events import EventBus
from ..security.crypto import KeychainKeyProvider, KeyProvider
from ..security.template_store import TemplateStore
from ..service import AssistantService
from ..voice_service import VoiceService

log = logging.getLogger(__name__)


class ProfileIn(BaseModel):
    owner_name: str = Field(min_length=1, max_length=40)
    assistant_name: str = Field(min_length=1, max_length=24)

    @field_validator("owner_name", "assistant_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("must not be blank")
        return v


def create_app(
    settings: Settings | None = None,
    keys: KeyProvider | None = None,
    engine: FaceEngine | None = None,
    camera: Camera | None = None,
    speaker_engine: SpeakerEngine | None = None,
    mic: Microphone | None = None,
    vad_factory=None,
    voice: bool = True,
) -> FastAPI:
    s = settings or get_settings()
    db = Database(s.db_path)
    store = TemplateStore(s.templates_dir, keys or KeychainKeyProvider(s.keychain_service))
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
            **extra,
        )

    svc = AssistantService(
        s,
        db,
        store,
        bus,
        engine or FaceEngine(s.models_dir, s.face_model_pack),
        camera or Camera(s.camera_index),
        voice_factory=make_voice if voice else None,
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
        if svc.auth_public()["state"] != "approved":
            raise HTTPException(403, "owner verification required")

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
