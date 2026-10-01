import logging
import logging.handlers
import os
import sys
import threading
import time

import uvicorn

from .api.app import create_app
from .audio import mic
from .camera.capture import request_permission
from .config import get_settings
from .database.db import Database
from .netguard import NetGuard


def _exit_with_parent(on_exit=lambda: None) -> None:
    """If the desktop shell dies without cleaning up, don't linger holding the camera
    (or leave the AI models loaded)."""
    parent = os.getppid()
    while True:
        time.sleep(2)
        if os.getppid() != parent:
            try:
                on_exit()
            finally:
                os._exit(0)


def _on_parent_exit(app_holder: list) -> None:
    """The desktop shell is gone: unload the models and stop an Ollama server we started."""
    brain = app_holder[0].state.svc.brain if app_holder else None
    if brain is not None:
        brain.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO: Gmail searches ("from:Rahul") and Drive queries would land
    # in backend.log
    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = get_settings()
    if getattr(sys, "frozen", False):
        # the packaged app has no terminal: keep a small log for troubleshooting (no biometric values are logged)
        logs = s.data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(logs / "backend.log", maxBytes=2 << 20, backupCount=2)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
    if s.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Refusing to bind to a non-loopback address")
    app_holder: list = []
    if os.environ.get("JARVIS_WATCH_PARENT") == "1":
        threading.Thread(target=_exit_with_parent, args=(lambda: _on_parent_exit(app_holder),), daemon=True).start()
    # every model is on disk: libraries must not try to download or phone home
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    # offline guard (on by default; the owner can allow the network in Privacy)
    prefs_db = Database(s.db_path)
    guard = NetGuard(offline=lambda: prefs_db.get("pref.privacy.offline", "True") == "True")
    guard.install()
    # macOS permission prompts must come from the main thread; voice-only (the default) never
    # touches the camera, not even to ask for permission
    if s.face_auth:
        request_permission(s.camera_index)
    mic.request_permission()
    # Desktop/Documents/Downloads are guarded per app: touching them now makes macOS
    # ask once, instead of file search silently finding nothing later
    from .tools.files import FileSearch

    for folder in FileSearch(prefs_db).folders():
        FileSearch.access(folder)
    app = create_app(s, guard=guard)
    app_holder.append(app)
    uvicorn.run(app, host=s.host, port=s.port, log_level="warning")


if __name__ == "__main__":
    main()
