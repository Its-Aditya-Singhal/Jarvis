"""Proof that the Mac's own user is at the keyboard, for when the voiceprint can't say so.

Replacing the voiceprint normally needs a recent match of the current one. If that voiceprint
no longer recognises its owner (an old enrollment, a new microphone), that check can never pass.
macOS's own administrator prompt is the way back in: the password (or the fingerprint, where
the Mac offers it there) is typed into the system dialog, never into JARVIS, and nothing about
it reaches this process except "allowed" or "cancelled". It runs ``/usr/bin/true`` and nothing else.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable

log = logging.getLogger(__name__)

TIMEOUT_S = 120.0


def _script(reason: str) -> str:
    reason = reason.replace("\\", "").replace('"', "'")[:200]
    return f'do shell script "/usr/bin/true" with prompt "{reason}" with administrator privileges'


def confirm_mac_user(reason: str, run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                     platform: str = sys.platform) -> bool:
    """Ask macOS for the Mac user's password. True only if the system accepted it."""
    if platform != "darwin":
        return False
    try:
        r = run(["/usr/bin/osascript", "-e", _script(reason)], capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Mac password prompt failed (%s)", exc.__class__.__name__)
        return False
    return r.returncode == 0
