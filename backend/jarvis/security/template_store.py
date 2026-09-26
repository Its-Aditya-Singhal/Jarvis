"""Encrypted storage of biometric templates (embeddings only, never images)."""

from __future__ import annotations

import io
import os
from pathlib import Path

import numpy as np

from .crypto import KeyProvider, seal, unseal


class TemplateStore:
    def __init__(self, directory: Path, keys: KeyProvider):
        self.directory = Path(directory)
        self.keys = keys

    def _path(self, modality: str) -> Path:
        return self.directory / f"{modality}.tmpl"

    def exists(self, modality: str) -> bool:
        return self._path(modality).exists()

    def save(self, modality: str, embeddings: np.ndarray) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        np.save(buf, np.asarray(embeddings, dtype=np.float32), allow_pickle=False)
        blob = seal(self.keys.get_key(), buf.getvalue(), modality.encode())
        tmp = self._path(modality).with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.chmod(tmp, 0o600)
        tmp.replace(self._path(modality))

    def load(self, modality: str) -> np.ndarray | None:
        path = self._path(modality)
        if not path.exists():
            return None
        raw = unseal(self.keys.get_key(), path.read_bytes(), modality.encode())
        return np.load(io.BytesIO(raw), allow_pickle=False)

    def delete(self, modality: str) -> None:
        self._path(modality).unlink(missing_ok=True)
