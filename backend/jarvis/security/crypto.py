"""Encryption at rest for biometric templates.

A random 256-bit key is created on first use and stored in the macOS Keychain
(via ``keyring``). Templates are sealed with AES-256-GCM, so a copied template
file is useless without the Keychain entry of this user account, and any
tampering is detected on load.
"""

from __future__ import annotations

import base64
import os
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12


class KeyProvider(Protocol):
    def get_key(self) -> bytes: ...
    def delete_key(self) -> None: ...


class KeychainKeyProvider:
    """Stores the data-encryption key in the OS keychain.

    The key is read once per launch and kept in memory. If the user denies the
    Keychain prompt, the denial is remembered until the next launch, so macOS
    doesn't ask again on every template or row that needs the key.
    """

    def __init__(self, service: str, account: str = "template-key"):
        self.service = service
        self.account = account
        self._key: bytes | None = None
        self._denied = False

    def get_key(self) -> bytes:
        import keyring
        from keyring.errors import KeyringLocked

        if self._key is not None:
            return self._key
        if self._denied:
            raise KeyringLocked("Keychain access was denied for this launch")
        try:
            stored = keyring.get_password(self.service, self.account)
        except KeyringLocked:
            self._denied = True
            raise
        if stored:
            self._key = base64.b64decode(stored)
            return self._key
        key = AESGCM.generate_key(bit_length=256)
        keyring.set_password(self.service, self.account, base64.b64encode(key).decode())
        self._key = key
        return key

    def has_key(self) -> bool:
        import keyring

        if self._key is not None or self._denied:
            return True
        return keyring.get_password(self.service, self.account) is not None

    def delete_key(self) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        self._key = None
        try:
            keyring.delete_password(self.service, self.account)
        except PasswordDeleteError:
            pass


class StaticKeyProvider:
    """In-memory key; used by tests only."""

    def __init__(self, key: bytes | None = None):
        self._key = key or AESGCM.generate_key(bit_length=256)

    def get_key(self) -> bytes:
        return self._key

    def has_key(self) -> bool:
        return True

    def delete_key(self) -> None:
        self._key = AESGCM.generate_key(bit_length=256)


def seal(key: bytes, plaintext: bytes, context: bytes) -> bytes:
    """Encrypt ``plaintext``; ``context`` is authenticated but not encrypted."""
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, context)


def unseal(key: bytes, blob: bytes, context: bytes) -> bytes:
    nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    return AESGCM(key).decrypt(nonce, ciphertext, context)
