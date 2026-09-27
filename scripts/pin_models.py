"""Pin the SHA-256 (and exact size) of every model file in backend/jarvis/model_manifest.json.

    backend/.venv/bin/python scripts/pin_models.py            # pin what isn't pinned yet
    backend/.venv/bin/python scripts/pin_models.py --check    # exit 1 if anything is unpinned

A file already in models/ (from scripts/download_models.py) is hashed where it is;
anything else is streamed from its URL and hashed without being kept. For a file
Hugging Face publishes a SHA-256 for, the two must agree. build_dmg.sh runs this,
so a release always ships with every file pinned.
"""

import hashlib
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "backend" / "jarvis" / "model_manifest.json"
MODELS = ROOT / "models"
sys.path.insert(0, str(ROOT / "backend"))

from jarvis.downloads import _published_sha  # noqa: E402


def hash_local(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest(), path.stat().st_size


def hash_remote(url: str) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60.0, connect=15.0)) as r:
        r.raise_for_status()
        published = _published_sha(r.headers)
        for chunk in r.iter_bytes(1 << 20):
            h.update(chunk)
            n += len(chunk)
    digest = h.hexdigest()
    if published and published != digest:
        raise SystemExit(f"{url}: downloaded SHA-256 {digest} differs from the published {published}")
    return digest, n


def main() -> int:
    raw = json.loads(MANIFEST.read_text())
    unpinned = [(p["id"], f) for p in raw["packs"] for f in p["files"] if not f.get("sha256")]
    if "--check" in sys.argv:
        for pid, f in unpinned:
            print(f"unpinned: {pid} {f['path']}")
        return 1 if unpinned else 0
    for pid, f in unpinned:
        local = MODELS / f["path"]
        if local.is_file() and not f.get("extract"):
            digest, size = hash_local(local)
            src = "local copy"
        else:
            print(f"hashing {f['url']} …", flush=True)
            digest, size = hash_remote(f["url"])
            src = "download"
        f["sha256"], f["size"] = digest, size
        print(f"pinned {pid} {f['path']}: {digest} ({size} bytes, from {src})")
    MANIFEST.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
