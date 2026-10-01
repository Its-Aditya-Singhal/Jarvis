"""Small secrets (the Gemini API key, Google sign-in tokens), sealed with the Keychain key.

They live in the database encrypted (AES-256-GCM with the key kept in the macOS Keychain),
are never logged, and are never sent back to the UI: the UI only learns whether one is set and
its last four characters. A factory reset destroys the key, so they can't be read afterwards.
"""

from __future__ import annotations

import base64
import logging

from ..database.db import Database
from .crypto import KeyProvider, seal, unseal

log = logging.getLogger(__name__)


class Secrets:
    def __init__(self, db: Database, keys: KeyProvider):
        self.db = db
        self.keys = keys

    def _key(self, name: str) -> str:
        return f"secret.{name}"

    def get(self, name: str) -> str | None:
        raw = self.db.get(self._key(name))
        if not raw:
            return None
        try:
            return unseal(self.keys.get_key(), base64.b64decode(raw), name.encode()).decode()
        except Exception:
            log.warning("secret %s could not be decrypted with this Mac's key", name)
            return None

    def set(self, name: str, value: str | None) -> None:
        if not value:
            self.db.set(self._key(name), "")
            return
        blob = seal(self.keys.get_key(), value.encode(), name.encode())
        self.db.set(self._key(name), base64.b64encode(blob).decode())

    def has(self, name: str) -> bool:
        return bool(self.get(name))

    def hint(self, name: str) -> str | None:
        """'…ab12' for the UI, or None when not set."""
        v = self.get(name)
        return f"…{v[-4:]}" if v and len(v) >= 8 else ("set" if v else None)


# copying a key from a web page can bring along spaces, line breaks, zero-width characters or quotes
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00a0"))


def clean_api_key(raw: str, max_len: int) -> str | None:
    """The key as pasted, minus what a copy from a web page adds; None if it can't be a key.

    Only the shape is checked here (one run of printable ASCII, a sane length): providers
    change their key formats, so the provider's own answer to Test decides if it works."""
    key = "".join(raw.translate(_INVISIBLE).split()).strip("\"'`“”‘’")
    if not key:
        return ""
    if not 16 <= len(key) <= max_len or not all(33 <= ord(c) <= 126 for c in key):
        return None
    return key
