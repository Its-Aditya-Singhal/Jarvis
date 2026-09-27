import logging
import os
import threading
import time

import uvicorn

from .api.app import create_app
from .audio import mic
from .camera.capture import request_permission
from .config import get_settings
from .database.db import Database
from .netguard import NetGuard


def _exit_with_parent() -> None:
    """If the desktop shell dies without cleaning up, don't linger holding the camera."""
    parent = os.getppid()
    while True:
        time.sleep(2)
        if os.getppid() != parent:
            os._exit(0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    if s.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Refusing to bind to a non-loopback address")
    if os.environ.get("JARVIS_WATCH_PARENT") == "1":
        threading.Thread(target=_exit_with_parent, daemon=True).start()
    # every model is on disk: libraries must not try to download or phone home
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    # offline guard (on by default; the owner can allow the network in Privacy)
    prefs_db = Database(s.db_path)
    guard = NetGuard(offline=lambda: prefs_db.get("pref.privacy.offline", "True") == "True")
    guard.install()
    # macOS permission prompts must come from the main thread
    request_permission(s.camera_index)
    mic.request_permission()
    # Desktop/Documents/Downloads are guarded per app: touching them now makes macOS
    # ask once, instead of file search silently finding nothing later
    from .tools.files import FileSearch

    for folder in FileSearch(prefs_db).folders():
        FileSearch.access(folder)
    uvicorn.run(create_app(s, guard=guard), host=s.host, port=s.port, log_level="warning")


if __name__ == "__main__":
    main()
