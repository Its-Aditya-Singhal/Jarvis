"""Which app macOS holds responsible for this process.

Camera, microphone and folder permissions belong to an app, not to the Python
backend: the packaged JARVIS.app, or while developing whatever launched it
(Terminal, iTerm, VS Code, Claude…). Fix instructions name that app.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

_APP = re.compile(r"/([^/]+)\.app/Contents/")
NAMES = {"Code": "Visual Studio Code", "iTerm2": "iTerm"}  # bundle name -> name in System Settings


@lru_cache(maxsize=1)
def responsible_app() -> str:
    """The outermost .app among this process's ancestors (the one macOS asks
    about), or "JARVIS" when none can be found."""
    try:
        import psutil

        proc = psutil.Process(os.getpid())
        found = None
        while proc is not None and proc.pid > 1:
            try:
                m = _APP.search(proc.exe())
            except (psutil.Error, OSError):
                m = None
            if m:
                found = m.group(1)
            proc = proc.parent()
        return NAMES.get(found, found) if found else "JARVIS"
    except Exception:
        return "JARVIS"
