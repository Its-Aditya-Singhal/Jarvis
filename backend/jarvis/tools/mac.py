"""Everyday Mac controls: quit apps, open folders and websites, volume, media,
battery, locking the screen, screenshots, brightness, dark mode, System
Settings pages, and the clipboard.

Nothing here runs a shell or passes model text to one: apps are quit through
AppKit's polite ``terminate`` (the app can still ask to save), folders must be
known locations or the owner's allowed search folders, websites must be
http(s) URLs or become a search, and AppleScript only receives fixed scripts
with values passed as argv. System Settings pages come from a fixed list, and
dictated text is typed by pasting it (the owner's clipboard is put back after).
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import tempfile
import time
import unicodedata
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, urlparse

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
# "play believer on youtube": that site's search results
SITE_SEARCH = {
    "youtube": ("YouTube", "https://www.youtube.com/results?search_query={}"),
    "spotify": ("Spotify", "https://open.spotify.com/search/{}"),
}


# spoken page name -> System Settings pane (macOS 13+ ids; the Privacy_* anchors still open the right list)
_PRIVACY = "com.apple.preference.security?Privacy_"
SETTINGS_PAGES = {
    "wifi": "com.apple.wifi-settings-extension", "wi fi": "com.apple.wifi-settings-extension",
    "wireless": "com.apple.wifi-settings-extension", "internet": "com.apple.Network-Settings.extension",
    "network": "com.apple.Network-Settings.extension", "bluetooth": "com.apple.BluetoothSettings",
    "sound": "com.apple.Sound-Settings.extension", "audio": "com.apple.Sound-Settings.extension",
    "volume": "com.apple.Sound-Settings.extension",
    "display": "com.apple.Displays-Settings.extension", "displays": "com.apple.Displays-Settings.extension",
    "screen": "com.apple.Displays-Settings.extension", "brightness": "com.apple.Displays-Settings.extension",
    "monitor": "com.apple.Displays-Settings.extension",
    "battery": "com.apple.Battery-Settings.extension", "power": "com.apple.Battery-Settings.extension",
    "notifications": "com.apple.Notifications-Settings.extension",
    "notification": "com.apple.Notifications-Settings.extension",
    "focus": "com.apple.Focus-Settings.extension", "do not disturb": "com.apple.Focus-Settings.extension",
    "screen time": "com.apple.Screen-Time-Settings.extension",
    "general": "com.apple.systempreferences.GeneralSettings",
    "about": "com.apple.SystemProfiler.AboutExtension", "about this mac": "com.apple.SystemProfiler.AboutExtension",
    "software update": "com.apple.Software-Update-Settings.extension",
    "update": "com.apple.Software-Update-Settings.extension", "updates": "com.apple.Software-Update-Settings.extension",
    "storage": "com.apple.settings.Storage", "date and time": "com.apple.Date-Time-Settings.extension",
    "date time": "com.apple.Date-Time-Settings.extension",
    "date": "com.apple.Date-Time-Settings.extension", "time": "com.apple.Date-Time-Settings.extension",
    "language": "com.apple.Localization-Settings.extension",
    "language and region": "com.apple.Localization-Settings.extension",
    "sharing": "com.apple.Sharing-Settings.extension", "airdrop": "com.apple.Sharing-Settings.extension",
    "time machine": "com.apple.Time-Machine-Settings.extension", "backup": "com.apple.Time-Machine-Settings.extension",
    "login items": "com.apple.LoginItems-Settings.extension", "startup": "com.apple.LoginItems-Settings.extension",
    "appearance": "com.apple.Appearance-Settings.extension", "dark mode": "com.apple.Appearance-Settings.extension",
    "accessibility": "com.apple.Accessibility-Settings.extension",
    "control center": "com.apple.ControlCenter-Settings.extension",
    "siri": "com.apple.Siri-Settings.extension",
    "privacy": "com.apple.settings.PrivacySecurity.extension",
    "security": "com.apple.settings.PrivacySecurity.extension",
    "privacy and security": "com.apple.settings.PrivacySecurity.extension",
    "privacy security": "com.apple.settings.PrivacySecurity.extension",  # "&" is lost in transcription
    "desktop": "com.apple.Desktop-Settings.extension", "dock": "com.apple.Desktop-Settings.extension",
    "desktop and dock": "com.apple.Desktop-Settings.extension",
    "wallpaper": "com.apple.Wallpaper-Settings.extension", "background": "com.apple.Wallpaper-Settings.extension",
    "screen saver": "com.apple.ScreenSaver-Settings.extension", "screensaver": "com.apple.ScreenSaver-Settings.extension",
    "lock screen": "com.apple.Lock-Screen-Settings.extension",
    "touch id": "com.apple.Touch-ID-Settings.extension", "password": "com.apple.Touch-ID-Settings.extension",
    "passwords": "com.apple.Passwords-Settings.extension",
    "users": "com.apple.Users-Groups-Settings.extension", "users and groups": "com.apple.Users-Groups-Settings.extension",
    "internet accounts": "com.apple.Internet-Accounts-Settings.extension",
    "accounts": "com.apple.Internet-Accounts-Settings.extension",
    "keyboard": "com.apple.Keyboard-Settings.extension", "trackpad": "com.apple.Trackpad-Settings.extension",
    "mouse": "com.apple.Mouse-Settings.extension",
    "printers": "com.apple.Print-Scan-Settings.extension", "printer": "com.apple.Print-Scan-Settings.extension",
    "microphone": _PRIVACY + "Microphone", "mic": _PRIVACY + "Microphone", "camera": _PRIVACY + "Camera",
    "screen recording": _PRIVACY + "ScreenCapture", "location": _PRIVACY + "LocationServices",
    "location services": _PRIVACY + "LocationServices", "full disk access": _PRIVACY + "AllFiles",
    "files and folders": _PRIVACY + "FilesAndFolders", "automation": _PRIVACY + "Automation",
}
SETTINGS_TITLES = {  # how each pane is named aloud
    "com.apple.wifi-settings-extension": "Wi-Fi", "com.apple.Network-Settings.extension": "Network",
    "com.apple.BluetoothSettings": "Bluetooth", "com.apple.Sound-Settings.extension": "Sound",
    "com.apple.Displays-Settings.extension": "Displays", "com.apple.Battery-Settings.extension": "Battery",
    "com.apple.Notifications-Settings.extension": "Notifications", "com.apple.Focus-Settings.extension": "Focus",
    "com.apple.Screen-Time-Settings.extension": "Screen Time", "com.apple.systempreferences.GeneralSettings": "General",
    "com.apple.SystemProfiler.AboutExtension": "About", "com.apple.Software-Update-Settings.extension": "Software Update",
    "com.apple.settings.Storage": "Storage", "com.apple.Date-Time-Settings.extension": "Date & Time",
    "com.apple.Localization-Settings.extension": "Language & Region", "com.apple.Sharing-Settings.extension": "Sharing",
    "com.apple.Time-Machine-Settings.extension": "Time Machine", "com.apple.LoginItems-Settings.extension": "Login Items",
    "com.apple.Appearance-Settings.extension": "Appearance", "com.apple.Accessibility-Settings.extension": "Accessibility",
    "com.apple.ControlCenter-Settings.extension": "Control Centre", "com.apple.Siri-Settings.extension": "Siri",
    "com.apple.settings.PrivacySecurity.extension": "Privacy & Security",
    "com.apple.Desktop-Settings.extension": "Desktop & Dock", "com.apple.Wallpaper-Settings.extension": "Wallpaper",
    "com.apple.ScreenSaver-Settings.extension": "Screen Saver", "com.apple.Lock-Screen-Settings.extension": "Lock Screen",
    "com.apple.Touch-ID-Settings.extension": "Touch ID & Password", "com.apple.Passwords-Settings.extension": "Passwords",
    "com.apple.Users-Groups-Settings.extension": "Users & Groups",
    "com.apple.Internet-Accounts-Settings.extension": "Internet Accounts",
    "com.apple.Keyboard-Settings.extension": "Keyboard", "com.apple.Trackpad-Settings.extension": "Trackpad",
    "com.apple.Mouse-Settings.extension": "Mouse", "com.apple.Print-Scan-Settings.extension": "Printers & Scanners",
    _PRIVACY + "Microphone": "Microphone privacy", _PRIVACY + "Camera": "Camera privacy",
    _PRIVACY + "ScreenCapture": "Screen Recording privacy", _PRIVACY + "LocationServices": "Location Services",
    _PRIVACY + "AllFiles": "Full Disk Access", _PRIVACY + "FilesAndFolders": "Files and Folders",
    _PRIVACY + "Automation": "Automation privacy", _PRIVACY + "Accessibility": "Accessibility privacy",
}
SETTINGS_URL = "x-apple.systempreferences:"


def settings_page(spoken: str) -> str | None:
    """The System Settings pane id for a spoken page name ("wifi", "the bluetooth settings")."""
    s = to_latin(spoken.replace("&", " and "))
    s = re.sub(r"^(?:the|my|mac|system)\s+", "", s.strip())
    s = re.sub(r"\s+(?:settings?|preferences?|prefs|options|page|pane|panel|ki settings|ke settings|ka settings)$", "", s)
    s = re.sub(r"\s+", " ", s.replace("-", " ")).strip()
    return SETTINGS_PAGES.get(s) or SETTINGS_PAGES.get(s.rstrip("s"))


class Clipboard:
    """The general pasteboard, as text. Items a password manager marks as
    concealed (org.nspasteboard.ConcealedType) are reported, never read out."""

    CONCEALED = "org.nspasteboard.ConcealedType"

    @staticmethod
    def _pb():
        from AppKit import NSPasteboard

        return NSPasteboard.generalPasteboard()

    def text(self) -> str | None:
        from AppKit import NSPasteboardTypeString

        return self._pb().stringForType_(NSPasteboardTypeString)

    def concealed(self) -> bool:
        return self.CONCEALED in (self._pb().types() or [])

    def set_text(self, text: str) -> None:
        from AppKit import NSPasteboardTypeString

        pb = self._pb()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)


class Brightness:
    """The built-in display's brightness (0-1) through DisplayServices, which is
    what the brightness keys use on Apple Silicon. External monitors usually
    don't support it; both calls then return None / False."""

    def __init__(self) -> None:
        self._ds: Any = None  # None: not loaded yet, False: unavailable
        self._cg: Any = None

    def _load(self) -> bool:
        if self._ds is None:
            try:
                self._ds = ctypes.CDLL("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices")
                self._cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
                self._cg.CGMainDisplayID.restype = ctypes.c_uint32
                self._ds.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_float)]
                self._ds.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
            except (OSError, AttributeError):
                log.info("DisplayServices isn't available; brightness can't be changed")
                self._ds = False
        return bool(self._ds)

    def get(self) -> float | None:
        if not self._load():
            return None
        value = ctypes.c_float()
        err = self._ds.DisplayServicesGetBrightness(self._cg.CGMainDisplayID(), ctypes.byref(value))
        return None if err else float(value.value)

    def set(self, level: float) -> bool:
        if not self._load():
            return False
        return self._ds.DisplayServicesSetBrightness(self._cg.CGMainDisplayID(), ctypes.c_float(level)) == 0


def _screen_access() -> bool:
    """Whether macOS lets this process record the screen; asks once when it doesn't."""
    try:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
        return bool(cg.CGPreflightScreenCaptureAccess() or cg.CGRequestScreenCaptureAccess())
    except (OSError, AttributeError):
        return True  # older macOS: no separate permission


def clean_name(s: str) -> str:
    """Drop invisible marks some apps put in their names (WhatsApp has U+200E)."""
    return "".join(c for c in s if unicodedata.category(c) != "Cf").strip()


class MacControl:
    def __init__(self, runner: Callable[[list[str]], str] | None = None,
                 running: Callable[[], list] | None = None, extra_folders: Callable[[], list[Path]] = lambda: [],
                 home: Path | None = None, clipboard: Clipboard | None = None,
                 front: Callable[[], str | None] | None = None, brightness: Brightness | None = None,
                 screen_access: Callable[[], bool] | None = None):
        self.home = home or HOME
        self.folders = FOLDERS if home is None else known_folders(home)
        self._run = runner or self._subprocess
        self._running = running or self._running_apps
        self.extra_folders = extra_folders  # the owner's file-search folders
        self.clipboard = clipboard or Clipboard()
        self._front = front or self._front_app
        self.brightness = brightness or Brightness()
        self._screen_access = screen_access or _screen_access
        self._sleep = time.sleep

    @staticmethod
    def _subprocess(argv: list[str]) -> str:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=True).stdout.strip()

    @staticmethod
    def _running_apps() -> list:
        from AppKit import NSWorkspace

        # regular apps only (a Dock icon), not background helpers
        return [a for a in NSWorkspace.sharedWorkspace().runningApplications() if a.activationPolicy() == 0]

    @staticmethod
    def _front_app() -> str | None:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return clean_name(app.localizedName() or "") if app is not None else None

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
    def site_url(target: str, site: str = "") -> tuple[str, str]:
        """(url, what) for a site name, URL or search words (searched on ``site``: youtube | spotify)."""
        t = " ".join(target.split()).strip(" .")
        low = t.lower()
        if site.lower() in SITE_SEARCH:
            name, url = SITE_SEARCH[site.lower()]
            return url.format(quote_plus(t) if name == "YouTube" else quote(t)), f"“{t}” on {name}"
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

    # -- screen & display ---------------------------------------------------------------------
    def screenshot_folder(self) -> Path:
        """Where macOS saves screenshots (the owner may have changed it), else the Desktop."""
        try:
            loc = self._run(["defaults", "read", "com.apple.screencapture", "location"]).strip()
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
            loc = ""  # not set: the default
        path = Path(loc).expanduser() if loc else None
        return path if path is not None and path.is_dir() else self.home / "Desktop"

    def screenshot(self, to_clipboard: bool = False) -> Path | None:
        """Capture the whole screen silently. Returns the saved file (None when copied
        to the clipboard). Raises PermissionError until Screen Recording is allowed."""
        if not self._screen_access():
            raise PermissionError("screen recording")
        if to_clipboard:
            self._run(["screencapture", "-x", "-c"])
            return None
        folder = self.screenshot_folder()
        name = datetime.now().strftime("Screenshot %Y-%m-%d at %H.%M.%S")
        path = folder / f"{name}.png"
        n = 2
        while path.exists():
            path = folder / f"{name} ({n}).png"
            n += 1
        self._run(["screencapture", "-x", str(path)])
        return path

    def screen_image(self, max_px: int = 1600) -> bytes:
        """The whole screen as a JPEG (longest side ``max_px``) for screen help, kept only in memory: the
        temporary file is deleted at once. Raises PermissionError until Screen Recording is allowed."""
        if not self._screen_access():
            raise PermissionError("screen recording")
        fd, name = tempfile.mkstemp(prefix="jarvis-screen-", suffix=".jpg")
        os.close(fd)
        path = Path(name)
        try:
            self._run(["screencapture", "-x", "-t", "jpg", str(path)])
            try:
                self._run(["sips", "-Z", str(max_px), "-s", "formatOptions", "70", str(path)])  # smaller upload
            except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
                pass  # the full-size image still works
            data = path.read_bytes()
        finally:
            path.unlink(missing_ok=True)
        if not data:
            raise OSError("the screenshot came out empty")
        return data

    def get_brightness(self) -> int | None:
        b = self.brightness.get()
        return None if b is None else round(b * 100)

    def set_brightness(self, level: int) -> int | None:
        level = max(0, min(100, int(level)))
        return level if self.brightness.set(level / 100) else None

    def dark_mode(self) -> bool:
        try:
            return self._run(["defaults", "read", "-g", "AppleInterfaceStyle"]).strip() == "Dark"
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
            return False  # the key is missing in light mode

    def set_dark_mode(self, on: bool) -> None:
        # the first use makes macOS ask once to let the assistant control System Events
        script = ('on run argv\ntell application "System Events" to tell appearance preferences '
                  'to set dark mode to ((item 1 of argv) is "on")\nend run')
        self._run(["osascript", "-e", script, "on" if on else "off"])

    def open_settings(self, pane: str | None) -> None:
        if pane is not None and pane not in SETTINGS_TITLES:
            raise ValueError("unknown settings page")
        self._run(["open", SETTINGS_URL + (pane or "")])

    # -- clipboard & typing -------------------------------------------------------------------
    def front_app(self) -> str | None:
        return self._front()

    def type_text(self, text: str) -> str:
        """Paste ``text`` into the front app with ⌘V, then put the owner's clipboard
        back. Returns the app typed into. Raises LookupError when the front app is
        this assistant, PermissionError when macOS hasn't allowed keystrokes."""
        front = self.front_app()
        if not front or front.lower() in KEEP_RUNNING - {"finder"}:
            raise LookupError(front or "")
        clip = self.clipboard
        old = None if clip.concealed() else clip.text()  # a copied password isn't put back in plain view
        clip.set_text(text)
        try:
            self._command_key("v")
        finally:
            if old:
                self._sleep(0.4)  # let the app read the pasteboard first
                clip.set_text(old)
        return front

    def _command_key(self, key: str) -> None:
        """⌘ + ``key`` (a fixed letter) in the front app. PermissionError when macOS hasn't allowed it."""
        if key not in ("n", "v"):
            raise ValueError(key)
        try:
            self._run(["osascript", "-e", f'tell application "System Events" to keystroke "{key}" using command down'])
        except subprocess.CalledProcessError as exc:
            # 1002 / 1743: not allowed to send keystrokes (Accessibility) or control System Events
            if re.search(r"\b(1002|1743)\b|not allowed", (exc.stderr or "") + (exc.stdout or "")):
                raise PermissionError("accessibility") from exc
            raise

    # -- asking Claude / ChatGPT --------------------------------------------------------------
    def ask_ai(self, name: str, app: Path | None, prompt: str, web: str | None, home_url: str) -> str:
        """Put ``prompt`` into a new chat in Claude or ChatGPT without sending it.
        With the app: open it, ⌘N, paste. Else the website: ``web`` pre-fills the
        box; without one the prompt goes on the clipboard for the owner to paste.
        Returns "app", "web" or "clipboard"."""
        if app is not None and app.suffix == ".app" and app.is_dir():
            was_running = name in self.running()
            self._run(["open", str(app)])
            for _ in range(40 if not was_running else 12):  # up to ~10 s for a cold start
                if self.front_app() == name:
                    break
                self._sleep(0.25)
            else:
                self.clipboard.set_text(prompt)  # never paste into whatever else is in front
                return "clipboard"
            try:
                self._sleep(0.3 if was_running else 1.5)  # let the window finish loading
                self._command_key("n")
                self._sleep(0.5)
                self.type_text(prompt)
                return "app"
            except (PermissionError, LookupError):
                pass  # no Accessibility permission: fall back to the website below
        if web is not None:
            self.open_url(web.format(quote(prompt)))
            return "web"
        self.clipboard.set_text(prompt)
        self.open_url(home_url)
        return "clipboard"
