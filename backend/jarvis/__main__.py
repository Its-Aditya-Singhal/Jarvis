import logging
import os
import threading
import time

import uvicorn

from .api.app import create_app
from .audio import mic
from .camera.capture import request_permission
from .config import get_settings


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
    # macOS permission prompts must come from the main thread
    request_permission(s.camera_index)
    mic.request_permission()
    uvicorn.run(create_app(s), host=s.host, port=s.port, log_level="warning")


if __name__ == "__main__":
    main()
