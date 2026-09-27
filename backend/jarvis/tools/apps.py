"""Finds and opens installed Mac applications by (spoken) name.

Only ``.app`` bundles found in the standard application folders can be
opened, via ``open <bundle>`` with no arguments — no shell, no URLs, no
documents — so the LLM can't turn "open" into running arbitrary commands.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from rapidfuzz import fuzz

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
    "zoom": "zoom.us",
}
REFRESH_S = 300.0
MIN_SCORE = 80  # whole-name similarity when the first letter matches
STRICT_SCORE = 92  # otherwise


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
        words = set(re.findall(r"[a-z0-9]+", to_latin(spoken)))
        best, best_score = None, 0.0
        for key, n in by_norm.items():
            app_words = re.findall(r"[a-z0-9]+", to_latin(n))
            lone_brand = len(app_words) > 1 and words == {app_words[0]}
            if lone_brand:
                continue
            if len(app_words) > 1 and words and words <= set(app_words):
                # "chrome", "word", "visual studio": part of the name, but not a lone brand
                # word ("google" is the website, "microsoft" names no app)
                score = 100.0
            else:
                # a mis-heard or mis-typed whole name: close, and starting with the same letter
                # ("safary"), or very close ("gmail" is not "mail", "teams" is not "steam")
                r = fuzz.ratio(target, key)
                score = r if r >= STRICT_SCORE or (r >= MIN_SCORE and target[0] == key[0]) else 0.0
            if score > best_score:
                best, best_score = n, score
        if best is not None:
            return best, self._apps[best]
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
