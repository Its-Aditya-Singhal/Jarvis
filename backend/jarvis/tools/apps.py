"""Finds and opens installed Mac applications by (spoken) name.

Only ``.app`` bundles found in the standard application folders can be
opened, via ``open <bundle>`` with no arguments — no shell, no URLs, no
documents — so the LLM can't turn "open" into running arbitrary commands.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from rapidfuzz import fuzz, process

from ..speech.text import skeleton_word, to_latin

APP_DIRS = [
    Path("/Applications"),
    Path("/Applications/Utilities"),
    Path("/System/Applications"),
    Path("/System/Applications/Utilities"),
    Path("/System/Library/CoreServices/Applications"),
    Path.home() / "Applications",
]
EXTRA_BUNDLES = [Path("/System/Library/CoreServices/Finder.app")]
# what people say -> bundle name
ALIASES = {
    "vscode": "Visual Studio Code", "vs code": "Visual Studio Code", "code": "Visual Studio Code",
    "settings": "System Settings", "system preferences": "System Settings", "app store": "App Store",
    "whatsapp": "WhatsApp", "word": "Microsoft Word", "excel": "Microsoft Excel",
    "powerpoint": "Microsoft PowerPoint", "teams": "Microsoft Teams", "outlook": "Microsoft Outlook",
}
REFRESH_S = 300.0
MIN_SCORE = 80


def _norm(name: str) -> str:
    return to_latin(name).replace(" ", "")


class AppIndex:
    def __init__(self, dirs: list[Path] | None = None, opener=None):
        self.dirs = dirs or APP_DIRS
        self._apps: dict[str, Path] = {}
        self._built = 0.0
        self._open = opener or (lambda path: subprocess.run(["open", str(path)], check=True, timeout=10))

    def _scan(self) -> None:
        apps: dict[str, Path] = {}
        for d in self.dirs:
            if not d.is_dir():
                continue
            # the folder itself and one level of sub-folders (e.g. /Applications/Adobe Photoshop/…)
            for p in list(d.glob("*.app")) + list(d.glob("*/*.app")):
                apps.setdefault(p.stem, p)
        for p in EXTRA_BUNDLES:
            if p.is_dir():
                apps.setdefault(p.stem, p)
        self._apps = apps
        self._built = time.monotonic()

    def names(self) -> list[str]:
        if not self._apps or time.monotonic() - self._built > REFRESH_S:
            self._scan()
        return sorted(self._apps)

    def resolve(self, spoken: str) -> tuple[str, Path] | None:
        names = self.names()
        spoken = spoken.strip().removesuffix(".app")
        spoken = ALIASES.get(to_latin(spoken), spoken)
        target = _norm(spoken)
        if not target:
            return None
        by_norm = {_norm(n): n for n in names}
        if target in by_norm:
            n = by_norm[target]
            return n, self._apps[n]
        match = process.extractOne(target, list(by_norm), scorer=fuzz.WRatio)
        if match and match[1] >= MIN_SCORE:
            n = by_norm[match[0]]
            return n, self._apps[n]
        # names written in Devanagari ("फ़ाइंडर"): compare consonant skeletons
        sk = skeleton_word(target)
        same = [n for k, n in by_norm.items() if len(sk) >= 3 and skeleton_word(k) == sk]
        return (same[0], self._apps[same[0]]) if len(same) == 1 else None

    def open(self, spoken: str) -> str | None:
        """Opens the best match; returns its display name, or None if not installed."""
        found = self.resolve(spoken)
        if found is None:
            return None
        self._open(found[1])
        return found[0]
