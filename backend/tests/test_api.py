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


def _client(settings):
    app = create_app(settings, keys=StaticKeyProvider(), engine=FakeEngine(), camera=FakeCamera())
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
