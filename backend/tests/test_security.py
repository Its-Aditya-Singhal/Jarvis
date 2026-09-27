import numpy as np
import pytest
from cryptography.exceptions import InvalidTag

from jarvis.database.db import Database
from jarvis.security.crypto import StaticKeyProvider, seal, unseal
from jarvis.security.template_store import TemplateStore


def test_seal_roundtrip_and_context_binding():
    key = StaticKeyProvider().get_key()
    blob = seal(key, b"secret", b"face")
    assert unseal(key, blob, b"face") == b"secret"
    with pytest.raises(InvalidTag):
        unseal(key, blob, b"voice")  # a face template can't be swapped in as voice


def test_template_store_encrypts_and_detects_tampering(tmp_path):
    keys = StaticKeyProvider()
    store = TemplateStore(tmp_path, keys)
    emb = np.random.default_rng(0).normal(size=(10, 512)).astype(np.float32)
    store.save("face", emb)

    raw = (tmp_path / "face.tmpl").read_bytes()
    assert emb.tobytes()[:64] not in raw  # not stored in the clear
    assert (tmp_path / "face.tmpl").stat().st_mode & 0o777 == 0o600
    np.testing.assert_array_equal(store.load("face"), emb)

    tampered = bytearray(raw)
    tampered[-1] ^= 0xFF
    (tmp_path / "face.tmpl").write_bytes(bytes(tampered))
    with pytest.raises(InvalidTag):
        store.load("face")


def test_wrong_key_cannot_read(tmp_path):
    TemplateStore(tmp_path, StaticKeyProvider()).save("face", np.ones((2, 4), np.float32))
    with pytest.raises(InvalidTag):
        TemplateStore(tmp_path, StaticKeyProvider()).load("face")


def test_database_profile_and_events(tmp_path):
    db = Database(tmp_path / "x.db")
    db.set("owner_name", "Aditya")
    db.set("owner_name", "Adi")
    assert db.get("owner_name") == "Adi"
    db.add_security_event("unknown_face", "stranger", face_conf=0.1, blocked=True)
    [ev] = db.security_events()
    assert ev["kind"] == "unknown_face" and ev["blocked"] == 1


def test_data_folder_and_database_are_owner_only(tmp_path):
    import os
    import stat

    from jarvis.database.db import Database

    folder = tmp_path / "JarvisAssistant"
    db = Database(folder / "jarvis.sqlite3")
    db.set("owner_name", "A")
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(folder / "jarvis.sqlite3").st_mode) == 0o600
    db.close()
    os.chmod(folder / "jarvis.sqlite3", 0o644)  # created by an earlier version
    Database(folder / "jarvis.sqlite3").close()
    assert stat.S_IMODE(os.stat(folder / "jarvis.sqlite3").st_mode) == 0o600
