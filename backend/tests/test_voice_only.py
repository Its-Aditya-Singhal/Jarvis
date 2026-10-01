"""Voice-only sign-in (the default): no camera or face model, the voice guards commands."""

import time

import numpy as np
from test_api import FakeCamera, FakeEngine, H, _client, _voice_ready

from jarvis.config import Settings
from jarvis.security.crypto import StaticKeyProvider


class CountingCamera(FakeCamera):
    starts = 0

    def start(self):
        CountingCamera.starts += 1


class CountingEngine(FakeEngine):
    loads = 0

    def load(self):
        CountingEngine.loads += 1
        return True


def _voice_only(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", api_token="test-token", face_auth=False)


def _enroll_voice(svc) -> None:
    svc.store.save("voice", np.tile(np.eye(1, 192, dtype=np.float32), (6, 1)))


def test_default_is_voice_only(monkeypatch):
    monkeypatch.delenv("JARVIS_FACE_AUTH", raising=False)
    assert Settings().face_auth is False


def test_setup_without_a_face_and_no_camera(tmp_path):
    CountingCamera.starts = CountingEngine.loads = 0
    client, svc = _client(_voice_only(tmp_path), camera=CountingCamera(), engine=CountingEngine())
    with client:
        _voice_ready(svc)
        client.post("/api/setup/profile", headers=H, json={"owner_name": "Aditya", "assistant_name": "JARVIS"})
        # voice is required when the microphone works
        assert client.post("/api/setup/complete", headers=H).status_code == 400
        _enroll_voice(svc)
        r = client.post("/api/setup/complete", headers=H)
        assert r.status_code == 200, r.text
        st = r.json()
        assert st["face_auth"] is False and st["face_reenroll"] is None
        assert st["camera"]["status"] == "off"
        assert st["auth"]["state"] == "approved" and st["auth"]["level"] == 1
        assert st["models"]["face"] == "disabled" and st["models"]["liveness"] == "disabled"
        # a face scan can't be started at all
        assert client.post("/api/enroll/face/start", headers=H).status_code == 403
        assert svc.voice.mode == "verifying"
    assert CountingCamera.starts == 0 and CountingEngine.loads == 0


def test_level_two_needs_a_recent_voice_match(tmp_path):
    client, svc = _client(_voice_only(tmp_path))
    with client:
        _voice_ready(svc)
        client.post("/api/setup/profile", headers=H, json={"owner_name": "Aditya", "assistant_name": "JARVIS"})
        _enroll_voice(svc)
        client.post("/api/setup/complete", headers=H)
        t = svc.trust()
        assert t.level == 1 and t.blockers[2] == "voice_needed"
        assert svc.trust(screen=True).level == 2  # typed commands stand in for the voice (Settings)
        # voice re-enrollment needs the current voice, or the keyboard when typed commands are on
        assert client.post("/api/enroll/voice/start", headers=H).status_code == 200
        client.post("/api/enroll/voice/cancel", headers=H)
        svc.prefs.set("security.typed", "off")
        assert client.post("/api/enroll/voice/start", headers=H).status_code == 403
        svc.voice.auth.judge(0.9, 0.9, time.monotonic(), 2.0)
        t = svc.trust()
        assert t.level == 2 and t.l3_ready  # no liveness to wait for
        assert client.post("/api/enroll/voice/start", headers=H).status_code == 200
        # someone else speaking blocks level 2 until the owner speaks again
        svc.voice.auth.judge(0.1, 0.9, time.monotonic(), 2.0)
        assert svc.trust().level == 1


def test_restart_resumes_voice_sign_in(tmp_path):
    s, keys = _voice_only(tmp_path), StaticKeyProvider()
    client, svc = _client(s, keys=keys)
    with client:
        _voice_ready(svc)
        client.post("/api/setup/profile", headers=H, json={"owner_name": "Aditya", "assistant_name": "JARVIS"})
        _enroll_voice(svc)
        client.post("/api/setup/complete", headers=H)
    client, svc = _client(s, keys=keys)
    with client:
        end = time.monotonic() + 5
        while svc.mode != "verifying":
            assert time.monotonic() < end
            time.sleep(0.02)
        assert svc.owner_verified() and svc.voice.mode == "verifying"
