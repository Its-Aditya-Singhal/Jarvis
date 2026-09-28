"""Microphone diagnostics: the owner is told why the mic doesn't work, and how to fix it."""

import numpy as np
import pytest

from jarvis import health
from jarvis.audio import mic as mic_mod
from jarvis.audio.mic import Microphone, classify
from jarvis.auth.levels import Trust

from .test_api import FakeMic, H, _client


def test_errors_are_classified():
    assert classify(Exception("No input device matching 'AirPods'"), "AirPods") == "device_missing"
    assert classify(Exception("Error querying device -1"), None) == "open_failed"
    assert classify(Exception("No default input device available [PaErrorCode -9996]"), None) == "no_device"


def _active(monkeypatch, perm="granted"):
    m = Microphone()
    m.status = "active"
    monkeypatch.setattr(mic_mod, "permission", lambda: perm)
    clock = [100.0]
    monkeypatch.setattr(mic_mod.time, "monotonic", lambda: clock[0])
    return m, clock


def test_digital_silence_means_macos_is_withholding_the_audio(monkeypatch):
    m, clock = _active(monkeypatch, perm="denied")
    zeros = np.zeros((512, 1), dtype=np.float32)
    m._callback(zeros, 512, None, None)
    clock[0] += 2.0
    m._callback(zeros, 512, None, None)
    assert m.status == "active"  # a short gap is normal
    clock[0] += 1.5
    m._callback(zeros, 512, None, None)
    assert (m.status, m.cause) == ("error", "permission")


def test_silence_with_permission_granted_is_reported_as_silent_and_recovers(monkeypatch):
    m, clock = _active(monkeypatch, perm="granted")
    zeros = np.zeros((512, 1), dtype=np.float32)
    m._callback(zeros, 512, None, None)
    clock[0] += 3.5
    m._callback(zeros, 512, None, None)
    assert m.cause == "silent"
    # real sound arrives again (e.g. input unmuted): back to normal on its own
    m._callback(np.full((512, 1), 0.01, dtype=np.float32), 512, None, None)
    assert (m.status, m.cause, m.error) == ("active", None, None)


def test_quiet_room_noise_is_not_silence(monkeypatch):
    m, clock = _active(monkeypatch)
    noise = (np.random.default_rng(0).standard_normal((512, 1)) * 1e-5).astype(np.float32)
    for _ in range(5):
        m._callback(noise, 512, None, None)
        clock[0] += 1.0
    assert m.status == "active"


def test_an_unplugged_chosen_mic_falls_back_to_the_default():
    m = Microphone("AirPods")
    m.status, m.device_name = "active", "MacBook Pro Microphone"
    info = m.info()
    assert info["chosen"] == "AirPods" and info["fallback"] is True


@pytest.mark.parametrize("cause", ["permission", "silent", "no_device", "device_missing", "open_failed"])
def test_every_cause_has_a_fix_naming_the_right_app(cause):
    st = {"mic": {"status": "error", "cause": cause, "error": mic_mod.CAUSES[cause]}, "app": "Terminal"}
    (issue,) = [i for i in health.issues(st) if i["id"] == "mic"]
    assert mic_mod.CAUSES[cause] in issue["title"]
    if cause in ("permission", "silent"):
        assert "turn on Terminal" in issue["fix"]


class SwitchableMic(FakeMic):
    def __init__(self):
        self.used, self.retried = [], 0

    def use(self, device):
        self.used.append(device)

    def retry(self):
        self.retried += 1


def test_retry_and_choosing_a_mic(settings, monkeypatch):
    monkeypatch.setattr(mic_mod, "input_devices",
                        lambda: [{"name": "MacBook Pro Microphone", "default": True},
                                 {"name": "Microsoft Teams Audio", "default": False}])
    mic = SwitchableMic()
    client, svc = _client(settings, mic=mic)
    with client:
        r = client.get("/api/mic", headers=H).json()
        assert [d["name"] for d in r["devices"]] == ["MacBook Pro Microphone", "Microsoft Teams Audio"]
        assert client.post("/api/mic/retry", headers=H).status_code == 200 and mic.retried == 1
        # a different input could feed recordings to voice verification: level 2
        svc.trust = lambda now=None, screen=False: Trust(1, 0.95, blockers={2: "voice_needed", 3: "voice_needed"})
        assert client.put("/api/mic", headers=H, json={"device": "Microsoft Teams Audio"}).status_code == 403
        svc.trust = lambda now=None, screen=False: Trust(2, 0.99, l3_ready=True)
        assert client.put("/api/mic", headers=H, json={"device": "Not Plugged In"}).status_code == 400
        assert client.put("/api/mic", headers=H, json={"device": "Microsoft Teams Audio"}).status_code == 200
        assert mic.used == ["Microsoft Teams Audio"]
        assert svc.db.get("audio.mic_device") == "Microsoft Teams Audio"
        assert client.put("/api/mic", headers=H, json={"device": None}).status_code == 200
        assert mic.used[-1] is None and not svc.db.get("audio.mic_device")
