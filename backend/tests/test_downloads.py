"""First-run model download: resume, checksums, disk space, the offline guard, the API."""

import hashlib
import io
import socket
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jarvis.downloads import DownloadError, ModelDownloader, ModelFile, Pack, load_manifest
from jarvis.netguard import NetGuard


class Files(BaseHTTPRequestHandler):
    blobs: dict[str, bytes] = {}
    cut_after: int | None = None  # drop the connection after this many bytes (once)
    ignore_range = False
    publish_sha = False
    range_offset = 0  # answer a range this many bytes off from the one asked for
    requests: list[tuple[str, str | None]] = []

    def do_GET(self):
        data = self.blobs.get(self.path)
        rng = self.headers.get("Range")
        type(self).requests.append((self.path, rng))
        if data is None:
            self.send_response(404); self.end_headers(); return
        start = int(rng.split("=")[1].rstrip("-")) if rng and not self.ignore_range else 0
        if start >= len(data) and rng:
            self.send_response(416); self.end_headers(); return
        start = max(0, start + type(self).range_offset) if start else 0
        body = data[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(body)))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        if self.publish_sha:
            self.send_header("X-Linked-Etag", f'"{hashlib.sha256(data).hexdigest()}"')
        self.end_headers()
        cut = type(self).cut_after
        if cut is not None:
            type(self).cut_after = None
            self.wfile.write(body[:cut]); self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    Files.blobs, Files.cut_after, Files.ignore_range, Files.publish_sha, Files.requests = {}, None, False, False, []
    Files.range_offset = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Files)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def one(url: str, data: bytes, pinned=True, **kw) -> list[Pack]:
    return [Pack("p", "Pack", "", True, [ModelFile("a/b.bin", url + "/b.bin", len(data), sha(data) if pinned else "", **kw)])]


def run(d: ModelDownloader, packs=None) -> dict:
    d.start(packs)
    d.wait(10)
    return d.status()


def test_downloads_verifies_and_reports_done(server, tmp_path):
    data = b"x" * 3_000_000
    Files.blobs["/b.bin"] = data
    d = ModelDownloader(tmp_path, one(server, data), disk_free=lambda p: 10**12)
    assert d.needed() == ["p"] and d.status()["needed_bytes"] == len(data)
    st = run(d)
    assert st["state"] == "done" and st["needed"] == [] and (tmp_path / "a/b.bin").read_bytes() == data
    assert not (tmp_path / "a/b.bin.part").exists()
    assert d.start()["started"] is False  # nothing left


def test_an_interrupted_download_resumes_where_it_stopped(server, tmp_path):
    data = bytes(range(256)) * 20_000
    Files.blobs["/b.bin"] = data
    Files.cut_after = 1_000_000
    d = ModelDownloader(tmp_path, one(server, data), disk_free=lambda p: 10**12)
    st = run(d)
    assert st["state"] == "error" and "Resume" in st["error"]
    assert (tmp_path / "a/b.bin.part").stat().st_size == 1_000_000 and d.status()["packs"][0]["partial"]
    st = run(d)
    assert st["state"] == "done" and (tmp_path / "a/b.bin").read_bytes() == data
    assert Files.requests[-1] == ("/b.bin", "bytes=1000000-")


def test_a_server_ignoring_the_range_restarts_cleanly(server, tmp_path):
    data = b"abc" * 500_000
    Files.blobs["/b.bin"] = data
    (tmp_path / "a").mkdir()
    (tmp_path / "a/b.bin.part").write_bytes(data[:1000])
    Files.ignore_range = True
    st = run(ModelDownloader(tmp_path, one(server, data), disk_free=lambda p: 10**12))
    assert st["state"] == "done" and (tmp_path / "a/b.bin").read_bytes() == data


def test_a_damaged_file_fails_its_checksum_and_is_deleted(server, tmp_path):
    good = b"good" * 1000
    Files.blobs["/b.bin"] = b"evil" * 1000
    st = run(ModelDownloader(tmp_path, one(server, good), disk_free=lambda p: 10**12))
    assert st["state"] == "error" and "checksum" in st["error"]
    assert not (tmp_path / "a/b.bin").exists() and not (tmp_path / "a/b.bin.part").exists()


def test_unpinned_files_are_checked_against_the_published_sha(server, tmp_path):
    data = b"hf" * 10_000
    Files.blobs["/b.bin"] = data
    Files.publish_sha = True
    st = run(ModelDownloader(tmp_path, one(server, data, pinned=False), disk_free=lambda p: 10**12))
    assert st["state"] == "done"
    # a server whose stated checksum doesn't match what arrived (changed in transit)
    orig = Files.do_GET

    def lying(self):
        self.send_response(200)
        self.send_header("X-Linked-Etag", '"' + "0" * 64 + '"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    Files.do_GET = lying
    try:
        st = run(ModelDownloader(tmp_path / "other", one(server, data, pinned=False), disk_free=lambda p: 10**12))
    finally:
        Files.do_GET = orig
    assert st["state"] == "error" and "checksum" in st["error"]


def test_not_enough_disk_space_refuses_before_downloading(server, tmp_path):
    data = b"z" * 1000
    Files.blobs["/b.bin"] = data
    d = ModelDownloader(tmp_path, one(server, data), disk_free=lambda p: 1000)
    r = d.start()
    assert r["started"] is False and "disk space" in r["reason"] and d.status()["state"] == "error"
    assert Files.requests == []


def test_zip_packs_unpack_only_the_listed_files(server, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("det.onnx", b"d" * 100)
        z.writestr("rec.onnx", b"r" * 100)
        z.writestr("../../evil.sh", b"rm -rf /")
    data = buf.getvalue()
    Files.blobs["/b.bin"] = data
    packs = one(server, data, extract="models/pack", members=["det.onnx", "rec.onnx"], unpacked=200)
    d = ModelDownloader(tmp_path, packs, disk_free=lambda p: 10**12)
    st = d.status()  # the download is the archive; the space check also counts what unpacking writes
    assert st["download_bytes"] == len(data) and st["needed_bytes"] == len(data) + 200
    assert run(d)["state"] == "done"
    assert sorted(p.name for p in (tmp_path / "models/pack").iterdir()) == ["det.onnx", "rec.onnx"]
    assert not (tmp_path / "a/b.bin").exists() and not list(tmp_path.rglob("evil.sh"))
    assert d.needed() == []


def test_cancel_keeps_the_partial_download(server, tmp_path):
    data = b"c" * 20_000_000
    Files.blobs["/b.bin"] = data
    d = ModelDownloader(tmp_path, one(server, data), disk_free=lambda p: 10**12)
    d.start()
    end = time.monotonic() + 5
    while not (tmp_path / "a/b.bin.part").exists() and time.monotonic() < end:
        time.sleep(0.01)
    d.cancel()
    d.wait(10)
    st = d.status()
    assert st["state"] in ("cancelled", "done")
    if st["state"] == "cancelled":
        assert (tmp_path / "a/b.bin.part").exists()
        assert run(d)["state"] == "done" and (tmp_path / "a/b.bin").read_bytes() == data


def test_only_manifest_hosts_are_contacted(tmp_path):
    packs = [Pack("p", "P", "", True, [ModelFile("x", "https://evil.example.com/x", 10, "")])]
    st = run(ModelDownloader(tmp_path, packs, disk_free=lambda p: 10**12))
    assert st["state"] == "error" and "evil.example.com" in st["error"]


def test_the_offline_guard_opens_download_hosts_for_the_download_thread_only():
    g = NetGuard(offline=lambda: True)
    g.install()
    try:
        with g.allow_download(("example.com",)):
            g.check("cdn.example.com", 443, "dns")  # allowed on this thread
            with pytest.raises(OSError, match="offline mode"):
                g.check("example.org", 443, "dns")
            g._resolved("cdn.example.com", [(2, 1, 6, "", ("93.184.215.14", 443))])
            g.check("93.184.215.14", 443, "connect")
            failed = []
            t = threading.Thread(target=lambda: failed.append(_blocked(g, "93.184.215.14")))
            t.start(); t.join()
            assert failed == [True]  # another thread can't ride along
        with pytest.raises(OSError, match="offline mode"):
            g.check("93.184.215.14", 443, "connect")  # closed again afterwards
    finally:
        g.uninstall()


def _blocked(g, host) -> bool:
    try:
        g.check(host, 443, "connect")
        return False
    except OSError:
        return True


def test_the_shipped_manifest_is_well_formed():
    packs = load_manifest()
    ids = [p.id for p in packs]
    assert len(ids) == len(set(ids)) and {"face", "liveness", "voice", "stt", "tts"} <= set(ids)
    for p in packs:
        for f in p.files:
            assert f.url.startswith("https://") and f.size > 0
            assert not f.sha256 or len(f.sha256) == 64
            assert ".." not in f.path and not f.path.startswith("/")
            assert f.extract is None or (".." not in f.extract and not f.extract.startswith("/"))
            assert all("/" not in m and ".." not in m for m in f.members)
    by = {p.id: p for p in packs}
    assert by["liveness"].files[0].sha256 == "87a9ac1dbb16a61eec212957e5095e62a8769c1e188af9b0198f253302c4afdb"
    mlx = ModelDownloader("/nonexistent", packs, platform_id="linux-x86_64")
    assert "stt_gpu" not in [p.id for p in mlx.packs]
    assert "stt_gpu" in [p.id for p in ModelDownloader("/nonexistent", packs, platform_id="darwin-arm64").packs]


def test_unknown_packs_are_refused(tmp_path):
    with pytest.raises(DownloadError):
        ModelDownloader(tmp_path, [], disk_free=lambda p: 10**12).start(["nope"])


def test_a_resume_answered_with_another_range_is_not_spliced(server, tmp_path):
    data = bytes(range(256)) * 4000
    Files.blobs["/b.bin"] = data
    (tmp_path / "a").mkdir()
    (tmp_path / "a/b.bin.part").write_bytes(data[:5000])
    Files.range_offset = 100  # a broken proxy or CDN: bytes 5100- for "bytes=5000-"
    d = ModelDownloader(tmp_path, one(server, data, pinned=False), disk_free=lambda p: 10**12)
    st = run(d)
    assert st["state"] == "error" and "wrong place" in st["error"]
    assert not (tmp_path / "a/b.bin.part").exists() and not (tmp_path / "a/b.bin").exists()
    Files.range_offset = 0
    assert run(d)["state"] == "done" and (tmp_path / "a/b.bin").read_bytes() == data


def test_an_archive_left_unpacked_is_unpacked_without_downloading_again(server, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("det.onnx", b"d" * 100)
    data = buf.getvalue()
    Files.blobs["/b.bin"] = data
    (tmp_path / "a").mkdir()
    (tmp_path / "a/b.bin").write_bytes(data)  # verified and moved into place, then the app quit mid-unpack
    d = ModelDownloader(tmp_path, one(server, data, extract="models/pack", members=["det.onnx"]),
                        disk_free=lambda p: 10**12)
    assert d.needed() == ["p"]
    assert run(d)["state"] == "done" and (tmp_path / "models/pack/det.onnx").read_bytes() == b"d" * 100
    assert Files.requests == [] and not (tmp_path / "a/b.bin").exists()


def test_a_damaged_leftover_archive_is_downloaded_again(server, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("det.onnx", b"d" * 100)
    data = buf.getvalue()
    Files.blobs["/b.bin"] = data
    (tmp_path / "a").mkdir()
    (tmp_path / "a/b.bin").write_bytes(b"x" * len(data))
    d = ModelDownloader(tmp_path, one(server, data, extract="models/pack", members=["det.onnx"]),
                        disk_free=lambda p: 10**12)
    assert run(d)["state"] == "done" and (tmp_path / "models/pack/det.onnx").exists()
    assert [r[0] for r in Files.requests] == ["/b.bin"]


def test_two_quick_starts_run_one_download(server, tmp_path):
    data = b"q" * 5_000_000
    Files.blobs["/b.bin"] = data
    d = ModelDownloader(tmp_path, one(server, data), disk_free=lambda p: 10**12)
    gate = threading.Barrier(8)
    results = []

    def press():
        gate.wait()
        results.append(d.start()["started"])

    threads = [threading.Thread(target=press) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    d.wait(10)
    assert results.count(True) == 1
    assert d.status()["state"] == "done" and (tmp_path / "a/b.bin").read_bytes() == data
