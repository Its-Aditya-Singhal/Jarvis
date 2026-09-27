import numpy as np
import pytest
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


def _client(settings, mic=None, **kw):
    args = dict(
        keys=StaticKeyProvider(), engine=FakeEngine(), camera=FakeCamera(), speaker_engine=FakeSpeaker(),
        mic=mic or FakeMic(), vad_factory=lambda: (lambda frame: 0.0), speech=False, llm=False, tools=False,
        memory=False,
    )
    app = create_app(settings, **{**args, **kw})
    return TestClient(app, base_url="http://127.0.0.1"), app.state.svc


def _voice_ready(svc, timeout: float = 10.0) -> None:
    """Voice models load in the background; the UI waits for this too (LOADING VOICE MODEL…)."""
    import time

    end = time.monotonic() + timeout
    while not (svc.voice and svc.voice.ready and svc.voice.mic.status == "active"):
        assert time.monotonic() < end, "voice pipeline never became ready"
        time.sleep(0.02)


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
        # voice works on this machine (once its models have loaded), so it is required
        import time
        end = time.monotonic() + 10
        while not svc.voice.available and time.monotonic() < end:
            time.sleep(0.02)
        r = client.post("/api/setup/complete", headers=H)
        assert r.status_code == 400 and "voice" in r.json()["detail"]
        svc.store.save("voice", np.eye(3, 192, dtype=np.float32))
        assert client.post("/api/setup/complete", headers=H).json()["setup_complete"] is True
        # once set up, nobody can overwrite the profile or re-enroll through setup
        assert client.post("/api/setup/profile", headers=H, json={"owner_name": "Mallory", "assistant_name": "X"}).status_code == 409
        assert client.post("/api/enroll/face/start", headers=H).status_code == 403  # needs a confirmed re-scan


def test_security_log_requires_verified_owner(settings):
    client, _ = _client(settings)
    with client:
        assert client.get("/api/security/events", headers=H).status_code == 403


def test_websocket_rejects_bad_token(settings):
    client, _ = _client(settings)
    with client:
        with client.websocket_connect("ws://127.0.0.1/ws?token=test-token") as ws:
            assert ws.receive_json()["type"] == "status"
        import pytest
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("ws://127.0.0.1/ws?token=bad") as ws:
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
        _voice_ready(svc)
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


def test_settings_and_privacy_endpoints(settings, tmp_path):
    from jarvis.auth.levels import Trust

    client, svc = _client(settings)
    with client:
        assert client.get("/api/privacy", headers=H).status_code == 403
        assert client.put("/api/settings/pref", headers=H, json={"key": "voice.speed", "value": 1.1}).status_code == 403
        svc.trust = lambda now=None: Trust(1, 0.95, blockers={2: "voice_needed", 3: "voice_needed"})
        inv = client.get("/api/privacy", headers=H).json()
        assert {i["id"] for i in inv["items"]} >= {"face", "voice", "security", "settings"}
        assert inv["network"]["offline"] is True
        # ordinary preferences at level 1, bad values refused
        assert client.put("/api/settings/pref", headers=H, json={"key": "voice.speed", "value": 1.1}).status_code == 200
        assert client.put("/api/settings/pref", headers=H, json={"key": "voice.speed", "value": 5}).status_code == 400
        assert client.put("/api/settings/pref", headers=H, json={"key": "nope", "value": 1}).status_code == 404
        # security settings and privacy actions need level 2
        assert client.put("/api/settings/pref", headers=H, json={"key": "security.face", "value": "strict"}).status_code == 403
        assert client.post("/api/privacy/factory_reset", headers=H).status_code == 403
        assert client.put("/api/settings/profile", headers=H,
                          json={"owner_name": "A", "assistant_name": "Friday"}).status_code == 403
        svc.trust = lambda now=None: Trust(2, 0.99, l3_ready=True)
        assert client.put("/api/settings/pref", headers=H, json={"key": "security.face", "value": "strict"}).json()["prefs"]
        # loosening opens a confirmation instead of applying
        r = client.put("/api/settings/pref", headers=H, json={"key": "security.face", "value": "standard"}).json()
        assert r["pending"] and svc.prefs.get("security.face") == "strict"
        assert client.post(f"/api/confirm/{r['pending']}", headers=H, json={"accept": True}).json()["ok"]
        assert svc.prefs.get("security.face") == "standard"
        assert client.post("/api/privacy/export", headers=H, json={"path": "rel.json"}).status_code == 400
        assert client.post("/api/privacy/bogus", headers=H).status_code == 404
        r = client.post("/api/privacy/clear_security_log", headers=H).json()
        assert r["ok"] and r["pending"]
        assert client.post("/api/privacy/clear_tools", headers=H).status_code == 409  # one at a time
        assert client.put("/api/settings/profile", headers=H,
                          json={"owner_name": " Aditya ", "assistant_name": "Friday"}).json()["assistant_name"] == "Friday"


def test_every_route_needs_the_launch_token(settings):
    """No REST route (except none) answers without the per-launch token."""
    from fastapi.routing import APIRoute

    client, _ = _client(settings)
    with client:
        routes = [r for r in client.app.routes if isinstance(r, APIRoute)]
        assert len(routes) > 40
        for r in routes:
            path = r.path.replace("{alarm_id}", "1").replace("{fact_id}", "1").replace("{pid}", "x") \
                .replace("{sid}", "x").replace("{action}", "export")
            for method in r.methods:
                res = client.request(method, path, json={})
                assert res.status_code == 401, f"{method} {r.path} answered {res.status_code} without the token"


def test_websocket_refuses_other_origins(settings):
    """A web page can open a WebSocket to 127.0.0.1 (no CORS for WebSockets): only the app's origins may."""
    from starlette.websockets import WebSocketDisconnect

    client, _ = _client(settings)
    with client:
        with client.websocket_connect("ws://127.0.0.1/ws?token=test-token", headers={"origin": "tauri://localhost"}) as ws:
            assert ws.receive_json()["type"] == "status"
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("ws://127.0.0.1/ws?token=test-token", headers={"origin": "https://evil.example"}) as ws:
                ws.receive_json()


def test_foreign_host_names_are_refused(settings):
    """DNS rebinding: a page on evil.example re-pointed at 127.0.0.1 sends Host: evil.example."""
    client, _ = _client(settings)
    with client:
        assert client.get("/api/status", headers={**H, "host": "evil.example:8765"}).status_code == 400
        assert client.get("/api/status", headers={**H, "host": "127.0.0.1:8765"}).status_code == 200
        assert client.get("/api/status", headers={**H, "host": "localhost:8765"}).status_code == 200


def test_tool_actions_from_the_ui_need_the_same_level_as_by_voice(settings):
    """Cancelling an alarm is level 2 by voice (tools.runner.LEVELS), so it is from the UI too;
    so is pushing items into Apple's apps."""
    from test_command_service import fake_trust

    from jarvis.tools.apple import AppleBridge

    client, svc = _client(settings, tools=True, apple=AppleBridge(run=lambda *a: ""))
    with client:
        state = {"level": 1}
        svc.trust = lambda now=None: fake_trust(state)
        aid = svc.tools.store.add_alarm("timer", __import__("datetime").datetime.now().replace(year=2030), "")
        r = client.post(f"/api/alarms/{aid}/cancel", headers=H)
        assert r.status_code == 403 and "level 2" in r.json()["detail"]
        assert client.post("/api/apple/sync", headers=H).status_code == 403
        state["level"] = 2
        assert client.post(f"/api/alarms/{aid}/cancel", headers=H).status_code == 200
        assert client.post("/api/apple/sync", headers=H).status_code == 200


def test_model_download_endpoints_and_restart(settings, tmp_path):
    import time

    from jarvis.downloads import ModelDownloader, ModelFile, Pack

    packs = [Pack("face", "Face", "", True, [ModelFile("f.bin", "http://127.0.0.1:9/f.bin", 4, "")])]
    dl = ModelDownloader(tmp_path / "m", packs, disk_free=lambda p: 10**12)
    restarted = []
    client, svc = _client(settings, downloader=dl, restart=lambda: restarted.append(True))
    with client:
        st = client.get("/api/status", headers=H).json()
        assert st["models_needed"] is True
        m = client.get("/api/models", headers=H).json()
        assert m["files"]["needed"] == ["face"] and "ollama" in m and m["ollama"]["install"]["brew"]
        assert client.post("/api/models/download", headers=H, json={"packs": ["nope"]}).status_code == 400
        (tmp_path / "m").mkdir()
        (tmp_path / "m" / "f.bin").write_bytes(b"done")  # as if downloaded
        assert client.get("/api/status", headers=H).json()["models_needed"] is False
        assert client.post("/api/models/restart", headers=H).json() == {"ok": True}
        end = time.monotonic() + 5
        while not restarted and time.monotonic() < end:
            time.sleep(0.02)
        assert restarted == [True]
        # once set up with every model present, only the verified owner may download
        svc.db.set("setup_complete", "1")
        assert client.post("/api/models/download", headers=H).status_code == 403
        assert client.post("/api/models/ollama/pull", headers=H, json={"model": "qwen2.5:7b"}).status_code == 403
        assert client.get("/api/models", headers=H).status_code == 200  # reading what's installed is fine
