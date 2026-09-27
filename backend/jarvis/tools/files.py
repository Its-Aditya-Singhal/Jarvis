"""File search, recent files and the Trash, limited to folders the owner has allowed.

Default folders: Documents, Desktop, Downloads. More can be added in
Settings; a folder must exist and must not be a system or credential
location. Only file names, dates and paths are used; contents are never read.
Moving to the Trash (never deleting) is the only change ever made to a file,
and only after the owner confirmed that exact file.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rapidfuzz import fuzz

from ..database.db import Database

DEFAULT_FOLDERS = ["~/Documents", "~/Desktop", "~/Downloads"]
KEY = "file_search_folders"
# never searchable, even if chosen
FORBIDDEN = [
    "~/Library", "~/.ssh", "~/.gnupg", "~/.aws", "~/.config", "~/.Trash",
    "/System", "/Library", "/private", "/usr", "/bin", "/sbin", "/etc", "/var", "/opt",
]
MAX_RESULTS = 10
MAX_SCAN = 20000  # entries looked at per request, so a huge folder can't stall a command
MAX_DEPTH = 4
# spoken kind -> file extensions (None: any file)
KINDS: dict[str, set[str] | None] = {
    "any": None,
    "pdf": {".pdf"},
    "image": {".png", ".jpg", ".jpeg", ".heic", ".heif", ".gif", ".webp", ".tiff", ".tif", ".bmp", ".svg", ".raw"},
    "screenshot": {".png", ".jpg", ".jpeg", ".heic"},
    "document": {".doc", ".docx", ".pages", ".txt", ".rtf", ".md", ".odt", ".pdf"},
    "spreadsheet": {".xls", ".xlsx", ".numbers", ".csv", ".ods"},
    "presentation": {".ppt", ".pptx", ".key", ".odp"},
    "video": {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm"},
    "audio": {".mp3", ".m4a", ".wav", ".aac", ".flac", ".aiff", ".ogg"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".dmg"},
}
# folders macOS treats as programs or installers: never looked inside, never trashed
BUNDLES = {".app", ".appex", ".bundle", ".framework", ".plugin", ".prefpane", ".kext", ".pkg", ".mpkg", ".photoslibrary"}


@dataclass
class Found:
    path: Path
    when: datetime  # when it arrived or last changed, whichever is later


def _trash_with_finder_api(path: Path) -> None:
    """Move to the Trash the way Finder does (Put Back works); no extra permission."""
    try:
        from Foundation import NSURL, NSFileManager
    except ImportError as exc:  # not on a Mac
        raise FolderError("the Trash is only available on macOS") from exc
    ok, _, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(str(path)), None, None)
    if not ok:
        raise FolderError(f"macOS couldn't move it to the Trash: {err.localizedDescription() if err else 'unknown error'}")


def _expand(p: str | Path) -> Path:
    return Path(p).expanduser().resolve()


class FolderError(ValueError):
    pass


class FileSearch:
    def __init__(self, db: Database, protected: list[Path] | None = None, runner=None,
                 trasher: Callable[[Path], None] | None = None,
                 stamp: Callable[[os.stat_result], float] | None = None):
        self.db = db
        self.protected = [_expand(p) for p in protected or []]  # e.g. the JARVIS data folder
        self._run = runner or self._mdfind
        self._trash = trasher or _trash_with_finder_api
        # when a file arrived or last changed: a download's creation time is when it was downloaded
        self._stamp = stamp or (lambda st: max(getattr(st, "st_birthtime", 0.0), st.st_mtime))

    # -- allowed folders -------------------------------------------------------------
    def folders(self) -> list[Path]:
        raw = self.db.get(KEY)
        items = json.loads(raw) if raw else DEFAULT_FOLDERS
        return [_expand(p) for p in items]

    def validate(self, folder: str | Path) -> Path:
        p = _expand(folder)
        if not p.is_dir():
            raise FolderError(f"{p} is not a folder")
        home = Path.home().resolve()
        if p == home or p == Path("/") or p == Path("/Volumes"):
            raise FolderError("choose a specific folder, not your whole home folder or disk")
        if not (p.is_relative_to(home) or p.is_relative_to(Path("/Volumes"))):
            raise FolderError("only folders in your home folder or on external drives can be added")
        if any(part.startswith(".") for part in p.relative_to(p.anchor).parts):
            raise FolderError("hidden folders can't be added")
        for bad in [_expand(f) for f in FORBIDDEN] + self.protected:
            if p == bad or p.is_relative_to(bad):
                raise FolderError(f"{p} is a protected system location")
        return p

    def set_folders(self, folders: list[str]) -> list[Path]:
        valid = []
        for f in folders:
            p = self.validate(f)
            if p not in valid:
                valid.append(p)
        self.db.set(KEY, json.dumps([str(p) for p in valid]))
        return valid

    @staticmethod
    def access(folder: Path) -> str:
        """ok | denied | missing. macOS guards Desktop, Documents and Downloads per app
        (Privacy & Security → Files and Folders); without that permission Spotlight
        silently returns nothing, so check by listing the folder. The first listing
        also makes macOS ask the user."""
        if not folder.is_dir():
            return "missing"
        try:
            with os.scandir(folder) as it:  # reading one entry is enough (and cheap)
                next(it, None)
            return "ok"
        except PermissionError:
            return "denied"
        except OSError:
            return "missing"

    def status(self) -> list[dict]:
        return [{"path": str(f), "access": self.access(f)} for f in self.folders()]

    def denied(self) -> list[Path]:
        return [f for f in self.folders() if self.access(f) == "denied"]

    def allowed(self, path: str | Path) -> bool:
        p = _expand(path)
        return any(p.is_relative_to(f) for f in self.folders())

    # -- search ------------------------------------------------------------------------
    @staticmethod
    def _mdfind(folder: Path, query: str) -> list[str]:
        out = subprocess.run(
            ["mdfind", "-onlyin", str(folder), query], capture_output=True, text=True, timeout=8
        )
        return [line for line in out.stdout.splitlines() if line]

    def search(self, query: str) -> list[Path]:
        # a leading "-" would make the query an mdfind option (-live never returns)
        query = " ".join(query.split())[:100].lstrip("-").strip()
        if not query:
            return []
        hits = self._search(query)
        if not hits:
            # Spotlight matches word prefixes: "invoices" misses "invoice_march.pdf"
            singular = " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in query.split())
            if singular != query:
                hits = self._search(singular)
        return hits

    def _search(self, query: str) -> list[Path]:
        hits: list[Path] = []
        for folder in self.folders():
            if self.access(folder) != "ok":
                continue
            for line in self._run(folder, query):
                p = Path(line)
                if any(part.startswith(".") for part in p.relative_to(folder).parts if p.is_relative_to(folder)):
                    continue
                if p.is_relative_to(folder) and p not in hits:
                    hits.append(p)
        # file-name matches first, then Spotlight's content matches
        hits.sort(key=lambda p: -fuzz.partial_ratio(query.lower(), p.name.lower()))
        return hits[:MAX_RESULTS]

    def reveal(self, path: str) -> None:
        if not self.allowed(path):
            raise FolderError("outside the allowed folders")
        p = _expand(path)
        if not p.exists():
            raise FolderError("that file is no longer there")
        try:
            subprocess.run(["open", "-R", str(p)], check=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise FolderError(f"Finder couldn't show it: {exc}") from exc

    # -- recent files ------------------------------------------------------------------------
    def recent(self, kind: str = "any", since: datetime | None = None, until: datetime | None = None,
               within: Path | None = None, query: str = "", limit: int = MAX_RESULTS) -> list[Found]:
        """Files in the allowed folders (or only under ``within``), newest first:
        "the PDF I downloaded yesterday" is kind=pdf, within=Downloads, since/until=yesterday."""
        exts = KINDS.get(kind)  # unknown kinds: any file
        words = [w for w in query.lower().split() if w]
        roots: list[Path] = []
        for f in self.folders():
            if within is None or f.is_relative_to(within):
                roots.append(f)
            elif within.is_relative_to(f):
                roots.append(within)
        found: list[Found] = []
        budget = [MAX_SCAN]

        def walk(folder: Path, depth: int) -> None:
            try:
                it = os.scandir(folder)
            except OSError:
                return
            with it:
                for e in it:
                    budget[0] -= 1
                    if budget[0] < 0:
                        return
                    if e.name.startswith(".") or e.is_symlink():
                        continue
                    ext = os.path.splitext(e.name)[1].lower()
                    if e.is_dir(follow_symlinks=False):
                        if ext not in BUNDLES and depth < MAX_DEPTH:
                            walk(Path(e.path), depth + 1)
                        continue
                    if exts is not None and ext not in exts:
                        continue
                    if kind == "screenshot" and not e.name.lower().startswith(("screenshot", "screen shot")):
                        continue
                    if words and not all(w in e.name.lower() for w in words):
                        continue
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    when = datetime.fromtimestamp(self._stamp(st))
                    if (since is None or when >= since) and (until is None or when < until):
                        found.append(Found(Path(e.path), when))

        for root in dict.fromkeys(roots):
            if self.access(root) == "ok":
                walk(root, 0)
        found.sort(key=lambda f: f.when, reverse=True)
        return found[:limit]

    # -- the Trash ---------------------------------------------------------------------------
    def trashable(self, path: str | Path) -> Path:
        """The file, if it may be moved to the Trash: a plain file in an allowed folder."""
        raw = Path(path).expanduser()
        if raw.is_symlink():
            raise FolderError("that's a shortcut (symlink), not a file")
        if not self.allowed(raw):
            raise FolderError("outside the allowed folders")
        p = _expand(raw)
        if not p.exists():
            raise FolderError("that file is no longer there")
        if not p.is_file() or any(s.lower() in BUNDLES for s in p.suffixes):
            raise FolderError("only single files can be moved to the Trash, not folders or apps")
        if any(part.startswith(".") for part in p.parts):
            raise FolderError("hidden files are left alone")
        for bad in self.protected:
            if p.is_relative_to(bad):
                raise FolderError(f"{p} is a protected location")
        return p

    def trash(self, path: str | Path) -> Path:
        p = self.trashable(path)  # checked again: the file may have changed since it was confirmed
        self._trash(p)
        return p
