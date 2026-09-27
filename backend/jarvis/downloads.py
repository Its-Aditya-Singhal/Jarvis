"""First-run model download: resumable, checksummed, with a disk-space check.

The models (about 1.7 GB) are not shipped inside the app. On first launch the
UI shows what is missing and downloads it here, in the backend, from the fixed
list in ``model_manifest.json``: nothing else can be fetched, and only the
hosts that list names are reachable, even with offline mode on (see
``NetGuard.allow_download``, which opens them for the download thread only).

- Resume: data goes to ``<file>.part``; a later attempt asks the server for the
  rest (HTTP Range) and hashes what is already there first.
- Checksums: every file is checked against its SHA-256 pin, or, for a file not
  pinned yet, against the SHA-256 the server publishes (Hugging Face's LFS
  ``X-Linked-Etag``). A mismatch deletes the download and reports it.
- Disk space: the whole remaining download plus unpacking and a margin must fit
  before anything starts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import re
import shutil
import sys
import threading
import time
import zipfile
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

MANIFEST = Path(__file__).with_name("model_manifest.json")
# every host the manifest's files (and their redirects) are served from
DOWNLOAD_DOMAINS = ("github.com", "githubusercontent.com", "huggingface.co", "hf.co")
MARGIN = 500 << 20  # keep this much disk free after the download
CHUNK = 1 << 20
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def this_platform() -> str:
    return f"{sys.platform}-{'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else platform.machine().lower()}"


@dataclass
class ModelFile:
    path: str
    url: str
    size: int
    sha256: str = ""
    extract: str | None = None
    members: list[str] = field(default_factory=list)
    unpacked: int = 0


@dataclass
class Pack:
    id: str
    title: str
    detail: str
    required: bool
    files: list[ModelFile]
    platform: str | None = None


def load_manifest(path: Path = MANIFEST) -> list[Pack]:
    raw = json.loads(path.read_text())
    return [Pack(id=p["id"], title=p["title"], detail=p.get("detail", ""), required=p.get("required", False),
                 platform=p.get("platform"), files=[ModelFile(**f) for f in p["files"]]) for p in raw["packs"]]


class DownloadError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


class ModelDownloader:
    def __init__(self, models_dir: Path, packs: list[Pack] | None = None, guard=None,
                 transport: httpx.BaseTransport | None = None, platform_id: str | None = None,
                 disk_free: Callable[[Path], int] | None = None, on_change: Callable[[], None] = lambda: None):
        self.dir = Path(models_dir)
        plat = platform_id or this_platform()
        self.packs = [p for p in (packs if packs is not None else load_manifest()) if p.platform in (None, plat)]
        self.guard = guard
        self.transport = transport
        self.disk_free = disk_free or (lambda p: shutil.disk_usage(p).free)
        self.on_change = on_change
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._state = "idle"  # idle | downloading | verifying | unpacking | done | cancelled | error
        self._error: str | None = None
        self._file: str | None = None
        self._done_bytes = 0  # finished files of this run
        self._file_bytes = 0  # the current file so far (including resumed data)
        self._total = 0
        self._speed = 0.0
        self._queued: list[str] = []
        self._last_emit = 0.0

    # -- what is on disk -----------------------------------------------------------------
    def installed(self, f: ModelFile) -> bool:
        if f.extract:
            d = self.dir / f.extract
            return d.is_dir() and all((d / m).is_file() for m in f.members)
        p = self.dir / f.path
        # a pinned size is exact; an estimate only says the file must not be empty
        return p.is_file() and (p.stat().st_size == f.size if f.sha256 else p.stat().st_size > 0)

    def missing(self, pack: Pack) -> list[ModelFile]:
        return [f for f in pack.files if not self.installed(f)]

    def pack(self, pid: str) -> Pack:
        for p in self.packs:
            if p.id == pid:
                return p
        raise KeyError(pid)

    def needed(self) -> list[str]:
        """Required packs that aren't fully on disk."""
        return [p.id for p in self.packs if p.required and self.missing(p)]

    def _remaining(self, files: list[ModelFile], unpacked: bool = True) -> int:
        """Bytes still to fetch, plus what unpacking will write when `unpacked` (the disk
        space it needs). Partial downloads count as done."""
        n = 0
        for f in files:
            part = self._part(f)
            n += max(0, f.size - (part.stat().st_size if part.is_file() else 0)) + (f.unpacked if unpacked else 0)
        return n

    def _part(self, f: ModelFile) -> Path:
        return (self.dir / f.path).with_name((self.dir / f.path).name + ".part")

    def free_bytes(self) -> int:
        d = self.dir
        while not d.exists() and d != d.parent:
            d = d.parent
        return self.disk_free(d)

    def status(self) -> dict:
        with self._lock:
            run = {"state": self._state, "error": self._error, "file": self._file,
                   "done_bytes": self._done_bytes + self._file_bytes, "total_bytes": self._total,
                   "speed_bps": round(self._speed), "queued": list(self._queued)}
        run["current"] = next((p.id for p in self.packs if any(f.path == run["file"] for f in p.files)), None)
        packs, disk = [], 0
        for p in self.packs:
            miss = self.missing(p)
            packs.append({"id": p.id, "title": p.title, "detail": p.detail, "required": p.required,
                          "installed": not miss, "size": sum(f.size for f in p.files),
                          "remaining": self._remaining(miss, unpacked=False),  # what's left to download
                          "partial": any(self._part(f).is_file() for f in miss)})
            if p.required and miss:
                disk += self._remaining(miss)
        need = [p for p in packs if p["required"] and not p["installed"]]
        # download_bytes is what the progress bar counts; needed_bytes adds unpacking, for the space check
        return {**run, "packs": packs, "needed": [p["id"] for p in need],
                "download_bytes": sum(p["remaining"] for p in need), "needed_bytes": disk,
                "free_bytes": self.free_bytes()}

    # -- running -----------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, pack_ids: list[str] | None = None) -> dict:
        """Download the given packs (default: the required ones still missing) in the background."""
        with self._lock:
            if self.running:
                return {"started": False, "reason": "a download is already running"}
        ids = pack_ids if pack_ids is not None else self.needed()
        try:
            packs = [self.pack(i) for i in ids]
        except KeyError as exc:
            raise DownloadError(f"unknown model pack {exc.args[0]}") from None
        files = [f for p in packs for f in self.missing(p)]
        if not files:
            with self._lock:
                self._state, self._error, self._queued = "done", None, []
            self.on_change()
            return {"started": False, "reason": "already downloaded"}
        need = self._remaining(files) + MARGIN
        free = self.free_bytes()
        if free < need:
            msg = f"Not enough disk space: {_gb(need)} needed, {_gb(free)} free. Free some space and try again."
            with self._lock:
                self._state, self._error = "error", msg
            self.on_change()
            return {"started": False, "reason": msg}
        with self._lock:
            self._cancel.clear()
            self._state, self._error, self._queued = "downloading", None, [p.id for p in packs]
            self._done_bytes, self._file_bytes, self._speed = 0, 0, 0.0
            self._total = sum(f.size for f in files)
            self._thread = threading.Thread(target=self._run, args=(files,), name="model-download", daemon=True)
            self._thread.start()
        self.on_change()
        return {"started": True}

    def cancel(self) -> None:
        """Stop after the current chunk; what arrived is kept for the next attempt."""
        self._cancel.set()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, files: list[ModelFile]) -> None:
        guard_ctx = self.guard.allow_download(DOWNLOAD_DOMAINS) if self.guard is not None else nullcontext()
        try:
            with guard_ctx, httpx.Client(transport=self.transport, follow_redirects=True,
                                         timeout=httpx.Timeout(30.0, connect=15.0),
                                         event_hooks={"request": [_only_download_hosts]}) as http:
                for f in files:
                    self._fetch(http, f)
                    with self._lock:
                        self._done_bytes += f.size
                        self._file_bytes = 0
            with self._lock:
                self._state, self._file, self._queued = "done", None, []
            log.info("model download finished")
        except Cancelled:
            with self._lock:
                self._state, self._file = "cancelled", None
        except Exception as exc:  # network, disk, checksum: shown to the user with a retry
            log.warning("model download failed: %s", exc)
            msg = str(exc) if isinstance(exc, DownloadError) else _friendly(exc)
            with self._lock:
                self._state, self._error, self._file = "error", msg, None
        self.on_change()

    def _fetch(self, http: httpx.Client, f: ModelFile) -> None:
        dest = self.dir / f.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = self._part(f)
        with self._lock:
            self._file, self._state, self._file_bytes = f.path, "downloading", 0
        self.on_change()
        digest = hashlib.sha256()
        have = part.stat().st_size if part.is_file() else 0
        if f.sha256 and have > f.size:
            part.unlink()
            have = 0
        if have:
            with part.open("rb") as fh:  # resume: hash what's already here first
                for chunk in iter(lambda: fh.read(CHUNK), b""):
                    digest.update(chunk)
        headers = {"Range": f"bytes={have}-"} if have else {}
        started, got = time.monotonic(), 0
        with http.stream("GET", f.url, headers=headers) as r:
            if r.status_code == 416 and have:  # the part is already complete
                published = _published_sha(r.headers)
            else:
                if r.status_code not in (200, 206):
                    raise DownloadError(f"Download of {Path(f.path).name} failed (HTTP {r.status_code}). Try again later.")
                if r.status_code == 200 and have:  # the server ignored the range: start over
                    digest, have = hashlib.sha256(), 0
                published = _published_sha(r.headers)
                with part.open("ab" if have else "wb") as out:
                    for chunk in r.iter_bytes():  # as it arrives: a dropped connection keeps every byte received
                        if self._cancel.is_set():
                            raise Cancelled
                        out.write(chunk)
                        digest.update(chunk)
                        got += len(chunk)
                        if f.sha256 and have + got > f.size:
                            raise DownloadError(f"{Path(f.path).name} is larger than expected: download refused")
                        self._progress(have + got, got, started)
        with self._lock:
            self._state = "verifying"
        self.on_change()
        expected = f.sha256 or published
        actual = digest.hexdigest()
        if expected and actual != expected:
            part.unlink(missing_ok=True)
            raise DownloadError(f"{Path(f.path).name} failed its checksum (the download was damaged or changed). "
                                "It was deleted: try again.")
        if not expected:
            log.info("%s has no published checksum; sha256 %s", f.path, actual)
        part.replace(dest)
        if f.extract:
            self._unpack(f, dest)

    def _unpack(self, f: ModelFile, archive: Path) -> None:
        with self._lock:
            self._state = "unpacking"
        self.on_change()
        assert f.extract is not None
        target = self.dir / f.extract
        tmp = target.with_name(target.name + ".unpacking")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        with zipfile.ZipFile(archive) as z:
            for m in f.members:  # only the listed files, by base name: no paths from the archive
                info = next((i for i in z.infolist() if Path(i.filename).name == m and not i.is_dir()), None)
                if info is None:
                    shutil.rmtree(tmp, ignore_errors=True)
                    raise DownloadError(f"{archive.name} is missing {m}")
                with z.open(info) as src, (tmp / m).open("wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
        shutil.rmtree(target, ignore_errors=True)
        tmp.replace(target)
        archive.unlink()

    def _progress(self, file_bytes: int, got: int, started: float) -> None:
        now = time.monotonic()
        with self._lock:
            self._file_bytes = file_bytes
            self._speed = got / max(now - started, 1e-3)
        if now - self._last_emit >= 0.5:
            self._last_emit = now
            self.on_change()


def _only_download_hosts(request: httpx.Request) -> None:
    """Redirects included, the download may only talk to the manifest's hosts (loopback for tests)."""
    host = request.url.host
    from .netguard import host_matches, is_local

    if not (is_local(host) or host_matches(host, DOWNLOAD_DOMAINS)):
        raise DownloadError(f"refusing to download from {host}")


def _published_sha(headers: httpx.Headers) -> str:
    """The SHA-256 a server states for the file: Hugging Face sends it for large (LFS) files."""
    for key in ("x-linked-etag", "etag"):
        v = headers.get(key, "").strip().strip('"').removeprefix("W/").strip('"').lower()
        if SHA256.match(v):
            return v
    return ""


def _friendly(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPError | OSError):
        host = ""
        req = getattr(exc, "request", None) if isinstance(exc, httpx.HTTPError) else None
        if req is not None:
            try:
                host = f" to {urlsplit(str(req.url)).hostname}"
            except Exception:
                pass
        return (f"The download was interrupted{host} ({type(exc).__name__}). Check the internet connection and "
                "press Resume: what already arrived is kept.")
    return f"Download failed: {exc}"


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"

