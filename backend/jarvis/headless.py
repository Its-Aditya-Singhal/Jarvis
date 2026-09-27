"""Run the backend with simulated hardware and models (no camera, mic, GPU or Ollama).

    python -m jarvis.headless            # http://127.0.0.1:8765, prints the API token

For the test suite and for working on the UI on any machine. Everything the
real app does runs, except that the camera, microphone, models and Mac
integrations are the fakes in :mod:`jarvis.fakes`, driven by a scene you can
change over the API:

    PUT  /api/dev/scene  {"person": "stranger"}      who is at the screen
    POST /api/dev/say    {"text": "Jarvis, what's the time?"}

Data goes to a fresh temporary folder unless ``JARVIS_DATA_DIR`` is set.
"""

from __future__ import annotations

import logging
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .api.app import create_app
from .config import Settings
from .fakes import (
    FakeApp,
    FakeCamera,
    FakeFaceEngine,
    FakeMac,
    FakeMicrophone,
    FakeOllama,
    FakeOllamaServer,
    FakePlayer,
    FakeSpeakerEngine,
    FakeSTT,
    FakeTTS,
    Scene,
    fake_apps_dir,
    fake_osascript,
    fake_vad,
)
from .netguard import NetGuard
from .perf import PerfMonitor
from .security.crypto import StaticKeyProvider
from .tools.apple import AppleBridge
from .tools.apps import AppIndex
from .tools.mac import MacControl


@dataclass
class Rig:
    """The simulated world around one headless app."""

    scene: Scene
    camera: FakeCamera
    face: FakeFaceEngine
    mic: FakeMicrophone
    speaker: FakeSpeakerEngine
    stt: FakeSTT
    tts: FakeTTS
    ollama: FakeOllama
    mac: FakeMac
    opened_apps: list


def build(settings: Settings, scene: Scene | None = None, ollama: FakeOllama | None = None,
          realtime_mic: bool = False, **overrides) -> tuple[FastAPI, Rig]:
    """A full app (every pipeline enabled) on fakes. ``overrides`` go to ``create_app``."""
    scene = scene or Scene()
    mac = FakeMac()
    opened: list = []
    rig = Rig(scene, FakeCamera(scene), FakeFaceEngine(scene), FakeMicrophone(scene, realtime_mic),
              FakeSpeakerEngine(scene), FakeSTT(scene), FakeTTS(scene), ollama or FakeOllama(), mac, opened)
    apps_root = Path(settings.data_dir).parent / "fake-mac"
    home = apps_root / "home"
    for d in ("Documents", "Downloads", "Desktop"):
        (home / d).mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = dict(
        keys=StaticKeyProvider(), engine=rig.face, camera=rig.camera, speaker_engine=rig.speaker, mic=rig.mic,
        vad_factory=fake_vad, stt=rig.stt, tts=rig.tts, player=FakePlayer(),
        apple=AppleBridge(run=fake_osascript), apps=AppIndex([fake_apps_dir(apps_root)], opener=opened.append),
        memory_client=rig.ollama, llm_client=rig.ollama, llm_server=FakeOllamaServer(),
        guard=NetGuard(), perf=PerfMonitor(lambda on_battery: None, power=lambda: (False, None)),
        mac=MacControl(runner=mac.run, running=mac.running, home=home),
    )
    kwargs.update(overrides)
    app = create_app(settings, **kwargs)
    svc = app.state.svc
    if svc.tools is not None and svc.tools.files is not None:
        svc.tools.mac.extra_folders = svc.tools.files.folders

    def instruction() -> str | None:
        e = svc.enrollment
        if svc.mode == "enrolling" and e is not None and e.current is not None:
            return e.current.key
        live = svc.live
        if live.state == "challenge" and live.session is not None:
            return live.session.step
        return None

    scene.instruction = instruction
    scene.can_speak = lambda: svc.speech is None or not svc.speech.muted()
    svc.bus.on("tts", lambda e: scene.said.append(e["text"]) if e.get("active") else None)
    app.state.rig = rig
    return app, rig


class SceneIn(BaseModel):
    person: Literal["owner", "stranger", "photo", "nobody"] | None = None
    bystanders: int | None = Field(default=None, ge=0, le=3)
    cooperative: bool | None = None
    blinking: bool | None = None
    frozen: bool | None = None


class SayIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    speaker: Literal["owner", "stranger"] = "owner"


def add_dev_routes(app: FastAPI, token: str) -> None:
    rig: Rig = app.state.rig

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if token and authorization != f"Bearer {token}":
            raise HTTPException(401, "invalid token")

    def scene_out() -> dict:
        sc = rig.scene
        return {"person": sc.person, "bystanders": sc.bystanders, "cooperative": sc.cooperative,
                "blinking": sc.blinking, "frozen": sc.frozen, "said": sc.said[-20:],
                "opened": [str(p) for p in rig.opened_apps[-20:]], "mac_calls": rig.mac.calls[-20:]}

    @app.get("/api/dev/scene", dependencies=[Depends(require_token)])
    def get_scene():
        return scene_out()

    @app.put("/api/dev/scene", dependencies=[Depends(require_token)])
    def set_scene(body: SceneIn):
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(rig.scene, k, v)
        return scene_out()

    @app.post("/api/dev/say", dependencies=[Depends(require_token)])
    def say(body: SayIn):
        rig.scene.say(body.text, body.speaker)
        return {"ok": True}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn

    data = os.environ.get("JARVIS_DATA_DIR") or tempfile.mkdtemp(prefix="jarvis-headless-")
    token = os.environ.get("JARVIS_API_TOKEN") or secrets.token_urlsafe(24)
    s = Settings(data_dir=Path(data), api_token=token)
    if s.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Refusing to bind to a non-loopback address")
    app, rig = build(s, realtime_mic=True)
    rig.mac.apps.append(FakeApp("WhatsApp"))
    add_dev_routes(app, token)
    print(f"JARVIS headless on http://{s.host}:{s.port}  data={data}\nAPI token: {token}", flush=True)
    uvicorn.run(app, host=s.host, port=s.port, log_level="warning")


if __name__ == "__main__":
    main()
