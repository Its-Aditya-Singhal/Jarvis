"""Everyday Mac controls: quit apps, open folders and websites, volume, media,
battery, locking the screen.

Nothing here runs a shell or passes model text to one: apps are quit through
AppKit's polite ``terminate`` (the app can still ask to save), folders must be
known locations or the owner's allowed search folders, websites must be
http(s) URLs or become a search, and AppleScript only receives fixed scripts
with values passed as argv.
"""

from __future__ import annotations

import logging
import re
import subprocess
import unicodedata
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from rapidfuzz import fuzz, process

from ..speech.text import to_latin

log = logging.getLogger(__name__)

HOME = Path.home()


def known_folders(home: Path) -> dict[str, Path]:
    """Spoken folder names → locations under ``home`` (and /Applications)."""
    return {
        "documents": home / "Documents", "document": home / "Documents", "docs": home / "Documents",
        "downloads": home / "Downloads", "download": home / "Downloads",
        "desktop": home / "Desktop",
        "pictures": home / "Pictures", "photos folder": home / "Pictures", "pics": home / "Pictures",
        "music": home / "Music", "songs folder": home / "Music",
        "movies": home / "Movies", "videos": home / "Movies", "video": home / "Movies",
        "home": home, "home folder": home, "user folder": home,
        "applications": Path("/Applications"), "apps folder": Path("/Applications"),
        "icloud": home / "Library/Mobile Documents/com~apple~CloudDocs",
        "icloud drive": home / "Library/Mobile Documents/com~apple~CloudDocs",
        "trash": home / ".Trash", "bin": home / ".Trash",
    }


FOLDERS = known_folders(HOME)
SITES = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.com", "gmail": "https://mail.google.com",
    "github": "https://github.com", "instagram": "https://www.instagram.com", "twitter": "https://x.com",
    "x": "https://x.com", "facebook": "https://www.facebook.com", "linkedin": "https://www.linkedin.com",
    "netflix": "https://www.netflix.com", "amazon": "https://www.amazon.in", "flipkart": "https://www.flipkart.com",
    "chatgpt": "https://chatgpt.com", "wikipedia": "https://www.wikipedia.org", "reddit": "https://www.reddit.com",
    "spotify": "https://open.spotify.com", "whatsapp web": "https://web.whatsapp.com", "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com", "drive": "https://drive.google.com", "google drive": "https://drive.google.com",
    "stack overflow": "https://stackoverflow.com", "stackoverflow": "https://stackoverflow.com",
}
# folders macOS treats as programs or installers: `open` would run them instead of showing them
BUNDLES = {".app", ".appex", ".bundle", ".framework", ".plugin", ".prefpane", ".kext", ".mpkg", ".pkg",
           ".saver", ".workflow", ".action", ".xpc", ".qlgenerator", ".mdimporter", ".osax", ".scpt", ".scptd"}


def is_bundle(path: Path) -> bool:
    return path.suffix.lower() in BUNDLES or path.resolve().suffix.lower() in BUNDLES


# never quit from a voice command
KEEP_RUNNING = {"finder", "jarvis", "loginwindow", "dock", "systemuiserver"}
PLAYERS = ("Spotify", "Music")


def clean_name(s: str) -> str:
    """Drop invisible marks some apps put in their names (WhatsApp has U+200E)."""
    return "".join(c for c in s if unicodedata.category(c) != "Cf").strip()


class MacControl:
    def __init__(self, runner: Callable[[list[str]], str] | None = None,
                 running: Callable[[], list] | None = None, extra_folders: Callable[[], list[Path]] = lambda: [],
                 home: Path | None = None):
        self.home = home or HOME
        self.folders = FOLDERS if home is None else known_folders(home)
        self._run = runner or self._subprocess
        self._running = running or self._running_apps
        self.extra_folders = extra_folders  # the owner's file-search folders

    @staticmethod
    def _subprocess(argv: list[str]) -> str:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=True).stdout.strip()

    @staticmethod
    def _running_apps() -> list:
        from AppKit import NSWorkspace

        # regular apps only (a Dock icon), not background helpers
        return [a for a in NSWorkspace.sharedWorkspace().runningApplications() if a.activationPolicy() == 0]

    # -- apps -----------------------------------------------------------------------------
    def running(self) -> list[str]:
        return [clean_name(a.localizedName() or "") for a in self._running()]

    def quit(self, spoken: str) -> tuple[str | None, list[str]]:
        """Politely quit the running app best matching ``spoken``.
        Returns (quit app name or None, running app names)."""
        apps = {clean_name(a.localizedName() or ""): a for a in self._running()}
        names = [n for n in apps if n.lower() not in KEEP_RUNNING]
        target = to_latin(spoken).replace(" app", "").strip()
        if not target or not names:
            return None, names
        norm = {n.lower().replace(" ", ""): n for n in names}
        key = target.replace(" ", "")
        name = norm.get(key)
        if name is None:
            m = process.extractOne(key, list(norm), scorer=fuzz.WRatio)
            name = norm[m[0]] if m and m[1] >= 80 else None
        if name is None:
            return None, names
        apps[name].terminate()  # like ⌘Q: the app may ask to save first
        return name, names

    # -- folders & web ----------------------------------------------------------------------
    def folder(self, spoken: str) -> Path | None:
        found = self._find_folder(spoken)
        return None if found is None or is_bundle(found) else found

    def _find_folder(self, spoken: str) -> Path | None:
        s = to_latin(spoken).strip()
        s = re.sub(r"^(my|the|meri|mera|mere)\s+", "", s)
        s = re.sub(r"\s+(folder|directory|dir|fold)$", "", s).strip()
        for key in (s, s + " folder"):
            if key in self.folders and self.folders[key].is_dir():
                return self.folders[key]
        roots = [p for p in self.extra_folders() if p.is_dir()]
        for p in roots:
            if p.name.lower() == s:
                return p
        # a folder one level inside Documents / Desktop / Downloads (or the home folder)
        for root in roots + [self.home]:
            try:
                for child in root.iterdir():
                    if child.is_dir() and not child.name.startswith(".") and child.name.lower() == s:
                        return child
            except OSError:  # e.g. macOS hasn't allowed access to that folder
                continue
        return None

    def open_folder(self, path: Path) -> None:
        if is_bundle(path) or not path.is_dir():
            raise ValueError("only folders can be opened")
        self._run(["open", str(path)])

    @staticmethod
    def site_url(target: str) -> tuple[str, str]:
        """(url, what) for a site name, URL or search words."""
        t = " ".join(target.split()).strip(" .")
        low = t.lower()
        if low in SITES:
            return SITES[low], t
        if re.fullmatch(r"(https?://)?[\w-]+(\.[\w-]+)+(/\S*)?", low):
            url = t if low.startswith("http") else "https://" + t
            if urlparse(url).scheme in ("http", "https"):
                return url, urlparse(url).netloc
        return f"https://www.google.com/search?q={quote_plus(t)}", f"a search for “{t}”"

    def open_url(self, url: str) -> None:
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError("only web addresses can be opened")
        self._run(["open", url])

    # -- sound & media ------------------------------------------------------------------------
    def volume(self) -> tuple[int, bool]:
        out = self._run(["osascript", "-e", "get volume settings"])
        vol = re.search(r"output volume:(\d+)", out)
        muted = "output muted:true" in out
        return (int(vol.group(1)) if vol else 50), muted

    def set_volume(self, level: int) -> int:
        level = max(0, min(100, int(level)))
        script = "on run argv\nset volume output volume (item 1 of argv as integer) without output muted\nend run"
        self._run(["osascript", "-e", script, str(level)])
        return level

    def mute(self, on: bool) -> None:
        self._run(["osascript", "-e", f"set volume {'with' if on else 'without'} output muted"])

    def media(self, action: str) -> str | None:
        """play | pause | toggle | next | previous on Spotify or Music. Returns the player used."""
        verbs = {"play": "play", "pause": "pause", "toggle": "playpause", "next": "next track",
                 "previous": "previous track"}
        if action not in verbs:
            raise ValueError(action)
        running = self.running()
        player = next((p for p in PLAYERS if p in running), None)
        if player is None:
            if action not in ("play", "toggle"):
                return None
            player = "Music"
        # the first use makes macOS ask once to let the assistant control the player
        script = f"on run argv\ntell application (item 1 of argv) to {verbs[action]}\nend run"
        self._run(["osascript", "-e", script, player])
        return player

    # -- system -----------------------------------------------------------------------------------
    def battery(self) -> tuple[int | None, bool]:
        out = self._run(["pmset", "-g", "batt"])
        m = re.search(r"(\d+)%", out)
        return (int(m.group(1)) if m else None), "AC Power" in out

    def lock(self) -> None:
        # display sleep locks the Mac when a password is required after sleep (the default)
        self._run(["pmset", "displaysleepnow"])
