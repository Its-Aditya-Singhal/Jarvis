"""Spotlight file search limited to folders the owner has allowed.

Default folders: Documents, Desktop, Downloads. More can be added in
Settings; a folder must exist and must not be a system or credential
location. Only file names/paths are returned; contents are never read.
"""

from __future__ import annotations

import json
import subprocess
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


def _expand(p: str | Path) -> Path:
    return Path(p).expanduser().resolve()


class FolderError(ValueError):
    pass


class FileSearch:
    def __init__(self, db: Database, protected: list[Path] | None = None, runner=None):
        self.db = db
        self.protected = [_expand(p) for p in protected or []]  # e.g. the JARVIS data folder
        self._run = runner or self._mdfind

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
        query = " ".join(query.split())[:100]
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
            if not folder.is_dir():
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
        subprocess.run(["open", "-R", str(_expand(path))], check=True, timeout=10)
