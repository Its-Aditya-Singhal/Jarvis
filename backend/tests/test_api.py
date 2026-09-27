import numpy as np
from fastapi.testclient import TestClient

from jarvis.api.app import create_app
from jarvis.security.crypto import StaticKeyProvider

H = {"Authorization": "Bearer test-token"}


class FakeCamera:
    status, error = "active", None

    def start(self): ...
    def stop(self): ...
    def latest(self, max_age_s=1.0): return None


class FakeEngine:
    ready, error = True, None

    def load(self): return True
    def analyze(self, frame): return []


class FakeSpeaker:
    ready, error = True, None

    def load(self): return True
    def embed(self, audio): return np.eye(1, 192, dtype=np.float32)[0]
    def embed_windows(self, audio): return [self.embed(audio)] * 3


class FakeMic:
    status, error, device_name, level = "active", None, "Test mic", 0.0

    def start(self): ...
    def stop(self): ...
    def drain(self): ...
    def read(self, timeout=0.5):
        import time
        time.sleep(0.05)
        return None


def _client(settings, mic=None):
    app = create_app(
        settings, keys=StaticKeyProvider(), engine=FakeEngine(), camera=FakeCamera(),
        speaker_engine=FakeSpeaker(), mic=mic or FakeMic(), vad_factory=lambda: (lambda frame: 0.0), speech=False, llm=False, tools=False,
    )
    return TestClient(app), app.state.svc


def test_token_required(settings):
    client, _ = _client(settings)
    with client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert client.get("/api/status", headers=H).json()["setup_complete"] is False


def test_setup_flow_and_lockdown(settings):
    client, svc = _client(settings)
    with client:
        r = client.post("/api/setup/profile", headers=H, json={"owner_name": "  Aditya ", "assistant_name": "FRIDAY"})
        assert r.json()["owner_name"] == "Aditya" and r.json()["assistant_name"] == "FRIDAY"
        # cannot finish before a face is enrolled
        assert client.post("/api/setup/complete", headers=H).status_code == 400
        svc.store.save("face", np.eye(3, 512, dtype=np.float32))
        # voice works on this machine, so it is required
        r = client.post("/api/setup/complete", headers=H)
        assert r.status_code == 400 and "voice" in r.json()["detail"]
        svc.store.save("voice", np.eye(3, 192, dtype=np.float32))
        assert client.post("/api/setup/complete", headers=H).json()["setup_complete"] is True
        # once set up, nobody can overwrite the profile or re-enroll through setup
        assert client.post("/api/setup/profile", headers=H, json={"owner_name": "Mallory", "assistant_name": "X"}).status_code == 409
        assert client.post("/api/enroll/face/start", headers=H).status_code == 409


def test_security_log_requires_verified_owner(settings):
    client, _ = _client(settings)
    with client:
        assert client.get("/api/security/events", headers=H).status_code == 403


def test_websocket_rejects_bad_token(settings):
    client, _ = _client(settings)
    with client:
        with client.websocket_connect("/ws?token=test-token") as ws:
            assert ws.receive_json()["type"] == "status"
        import pytest
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws?token=bad") as ws:
                ws.receive_json()


def test_setup_can_finish_without_voice_when_no_microphone(settings):
    mic = FakeMic()
    mic.status, mic.error = "error", "no microphone"
    client, svc = _client(settings, mic=mic)
    with client:
        client.post("/api/setup/profile", headers=H, json={"owner_name": "A", "assistant_name": "J"})
        assert client.post("/api/enroll/voice/start", headers=H).status_code == 503
        svc.store.save("face", np.eye(3, 512, dtype=np.float32))
        assert client.post("/api/setup/complete", headers=H).status_code == 200


def test_voice_reenrollment_requires_verified_owner(settings):
    client, svc = _client(settings)
    with client:
        client.post("/api/setup/profile", headers=H, json={"owner_name": "A", "assistant_name": "J"})
        assert client.post("/api/enroll/voice/start", headers=H).status_code == 200  # during setup
        client.post("/api/enroll/voice/cancel", headers=H)
        svc.store.save("face", np.eye(3, 512, dtype=np.float32))
        svc.store.save("voice", np.eye(3, 192, dtype=np.float32))
        client.post("/api/setup/complete", headers=H)
        # nobody is verified in front of the (fake) camera
        assert client.post("/api/enroll/voice/start", headers=H).status_code == 403
        status = client.get("/api/status", headers=H).json()
        assert status["voice_enrolled"] and status["mic"]["status"] == "active"
        assert "confidence" not in status["auth"]["voice"]


def test_voice_choice_at_setup_and_owner_only_change(settings):
    client, svc = _client(settings)
    with client:
        r = client.post("/api/setup/profile", headers=H, json={"owner_name": "A", "assistant_name": "J", "voice_gender": "male"})
        assert r.json()["voice_gender"] == "male"
        bad = client.post("/api/setup/profile", headers=H, json={"owner_name": "A", "assistant_name": "J", "voice_gender": "robot"})
        assert bad.status_code == 422
        # speech is disabled in this client, so a preview can't be played
        assert client.post("/api/speech/preview", headers=H, json={"gender": "female"}).status_code == 503
        # changing the voice later needs the verified owner
        assert client.put("/api/settings/voice", headers=H, json={"gender": "female"}).status_code == 403


def test_commands_and_model_settings_are_owner_only(settings):
    client, _ = _client(settings)
    with client:
        assert client.post("/api/command", headers=H, json={"text": "what time is it"}).status_code == 403
        assert client.get("/api/llm/models", headers=H).status_code == 403
        assert client.put("/api/settings/llm", headers=H, json={"model": "x"}).status_code == 403
        assert client.post("/api/command", json={"text": "hi"}).status_code == 401


def test_level_gated_endpoints(settings):
    from jarvis.auth.levels import Trust

    client, svc = _client(settings)
    with client:
        # nobody verified
        assert client.get("/api/fusion", headers=H).status_code == 403
        assert client.post("/api/confirm/abc", headers=H, json={"accept": True}).status_code == 403
        assert client.post("/api/fusion/retrain", headers=H).status_code == 403
        # the owner at the screen, but hasn't spoken recently: level 1
        svc.trust = lambda now=None: Trust(1, 0.95, blockers={2: "voice_needed", 3: "voice_needed"})
        info = client.get("/api/fusion", headers=H).json()
        assert info["source"] == "default" and info["test"]["auc"] > 0.98
        assert info["device_samples"] == {"owner": 0, "other": 0}
        r = client.post("/api/fusion/retrain", headers=H)
        assert r.status_code == 403 and "talk to me first" in r.json()["detail"]
        assert client.put("/api/settings/files", headers=H, json={"folders": []}).status_code == 403
        assert client.post("/api/confirm/abc", headers=H, json={"accept": True}).json()["ok"] is False
        # level 2
        svc.trust = lambda now=None: Trust(2, 0.99, l3_ready=True)
        r = client.post("/api/fusion/retrain", headers=H)
        assert r.status_code == 400 and "owner samples" in r.json()["detail"]
        assert client.post("/api/fusion/reset", headers=H).json()["source"] == "default"
