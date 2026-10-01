"""Google Drive (read-only): search, recent files, and a file's text for a summary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .auth import GoogleAuth

API = "https://www.googleapis.com/drive/v3"
FIELDS = "files(id,name,mimeType,modifiedTime,webViewLink)"
MAX_TEXT = 15000
# Google's own formats are exported as text; plain files are downloaded as they are
EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
PLAIN = ("text/", "application/json", "application/csv")
KIND = {
    "application/vnd.google-apps.document": "document", "application/vnd.google-apps.spreadsheet": "spreadsheet",
    "application/vnd.google-apps.presentation": "presentation", "application/vnd.google-apps.folder": "folder",
    "application/pdf": "PDF",
}


@dataclass
class DriveFile:
    id: str
    name: str
    mime: str
    modified: datetime | None
    link: str

    @property
    def kind(self) -> str:
        if self.mime in KIND:
            return KIND[self.mime]
        return self.mime.split("/")[0] if self.mime.startswith(("image/", "video/", "audio/")) else "file"

    @property
    def readable(self) -> bool:
        return self.mime in EXPORT or self.mime.startswith(PLAIN)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "kind": self.kind, "link": self.link,
                "modified": self.modified.isoformat(timespec="minutes") if self.modified else None}


def _file(f: dict) -> DriveFile:
    when = None
    if f.get("modifiedTime"):
        try:
            when = datetime.fromisoformat(str(f["modifiedTime"]).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
        except ValueError:
            pass
    return DriveFile(str(f.get("id") or ""), str(f.get("name") or "untitled"), str(f.get("mimeType") or ""), when,
                     str(f.get("webViewLink") or ""))


def _quote(text: str) -> str:
    """A value inside a Drive query string literal."""
    return text.replace("\\", "\\\\").replace("'", "\\'")


class Drive:
    def __init__(self, auth: GoogleAuth):
        self.auth = auth

    def _list(self, **params: Any) -> list[DriveFile]:
        r = self.auth.call("GET", f"{API}/files", "drive", params={"fields": FIELDS, "spaces": "drive", **params})
        return [_file(f) for f in r.json().get("files") or []]

    def search(self, query: str, n: int = 5) -> list[DriveFile]:
        q = _quote(" ".join(query.split())[:100])
        # names first (they are what people say), then contents
        by_name = self._list(q=f"trashed = false and name contains '{q}'", pageSize=n, orderBy="modifiedTime desc")
        if len(by_name) >= n:
            return by_name
        seen = {f.id for f in by_name}
        more = self._list(q=f"trashed = false and fullText contains '{q}'", pageSize=n)
        return (by_name + [f for f in more if f.id not in seen])[:n]

    def recent(self, n: int = 5) -> list[DriveFile]:
        return self._list(q="trashed = false and mimeType != 'application/vnd.google-apps.folder'",
                          pageSize=n, orderBy="modifiedTime desc")

    def text(self, f: DriveFile) -> str | None:
        """The file's text (Docs, Slides and Sheets exported; text files as they are), or None."""
        if f.mime in EXPORT:
            r = self.auth.call("GET", f"{API}/files/{f.id}/export", "drive", params={"mimeType": EXPORT[f.mime]})
        elif f.mime.startswith(PLAIN):
            r = self.auth.call("GET", f"{API}/files/{f.id}", "drive", params={"alt": "media"})
        else:
            return None
        return r.content[: MAX_TEXT * 4].decode("utf-8", "replace")[:MAX_TEXT]
