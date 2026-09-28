"""The whole service end to end on simulated hardware (jarvis.fakes): first-time setup,
face + liveness + voice verification, spoken and typed commands, strangers and spoofs,
and a confirmed deletion — through the real API, pipelines and auth logic."""

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jarvis.fakes import FakeOllama
from jarvis.headless import add_dev_routes, build

H = {"Authorization": "Bearer test-token"}


class Harness:
    def __init__(self, settings, ollama=None, camera="always"):
        self.app, self.rig = build(settings, ollama=ollama)
        self.svc = self.app.state.svc
        # most of these tests watch the camera the whole time (strangers, walking away)
        self.svc.prefs.set("security.camera", camera)
        self.scene = self.rig.scene
        self.svc._loop = lambda: None  # the test drives the face loop, on its own clock
        self.skew = 0.0
        self.svc.clock = self.clock
        self.client = TestClient(self.app, base_url="http://127.0.0.1")

    def clock(self) -> float:
        return time.monotonic() + self.skew

    def tick(self, n: int = 1, dt: float = 1 / 12) -> None:
        for _ in range(n):
            self.skew += dt
            self.svc._tick(self.clock())

    def tick_until(self, cond, max_ticks: int = 600) -> None:
        for _ in range(max_ticks):
            if cond():
                return
            self.tick()
        raise AssertionError("condition not reached while ticking the face loop")

    def wait_for(self, cond, timeout: float = 10.0) -> None:
        """Wait in real time; the face loop keeps running meanwhile, as in the app."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return
            self.svc._tick(self.clock())
            time.sleep(0.02)
        raise AssertionError("condition not reached in time")

    def get(self, path):
        return self.client.get(path, headers=H)

    def post(self, path, json=None):
        return self.client.post(path, headers=H, json=json)

    def wait_models(self):
        self.wait_for(lambda: self.svc.speech.stt.ready and self.svc.voice.ready and self.svc.engine.ready)

    def setup(self):
        """First-time setup through the API, with the owner following every prompt."""
        self.wait_models()
        r = self.post("/api/setup/profile", {"owner_name": "Aditya", "assistant_name": "Jarvis"})
        assert r.status_code == 200
        assert self.post("/api/enroll/face/start").status_code == 200
        self.tick_until(lambda: self.svc.face_enrolled)
        assert self.post("/api/enroll/voice/start").status_code == 200
        v = self.svc.voice
        for phrase in list(v.enrollment.items):
            before = v.enrollment.index
            self.scene.say(phrase.text, duration_s=2.4)
            self.wait_for(lambda before=before: (e := v.enrollment) is None or e.index > before)
        self.wait_for(lambda: self.svc.voice_enrolled)  # saved by the voice thread
        r = self.post("/api/setup/complete")
        assert r.status_code == 200 and r.json()["setup_complete"] is True

    def verify(self):
        self.tick_until(lambda: self.svc.effective_state() == "approved")

    def voice_verified(self):
        """The owner talks, so the voice counts towards level 2."""
        n = len(self.scene.said)
        self.scene.say("Jarvis, how are you doing today?")
        self.wait_for(lambda: len(self.scene.said) > n)
        self.wait_for(lambda: self.svc.trust().level >= 2)


@pytest.fixture
def h(settings):
    harness = Harness(settings)
    with harness.client:
        yield harness


def test_first_time_setup_and_live_unlock(h):
    h.setup()
    status = h.get("/api/status").json()
    assert status["face_enrolled"] and status["voice_enrolled"] and status["mode"] == "verifying"
    h.verify()  # face match, then the liveness challenge (the owner follows the prompts)
    assert h.svc.live.state == "passed"
    h.wait_for(lambda: any("Authentication approved. Hi Aditya" in s for s in h.scene.said))
    auth = h.get("/api/status").json()["auth"]
    assert auth["state"] == "approved" and auth["level"] >= 1 and "trust" in auth


def test_spoken_command_runs_a_tool_and_answers_aloud(h):
    h.setup()
    h.verify()
    h.voice_verified()
    h.scene.say("Jarvis, set a timer for 5 minutes")
    h.wait_for(lambda: any(a.kind == "timer" for a in h.svc.tools.store.alarms()))
    h.wait_for(lambda: any("timer" in s.lower() for s in h.scene.said[-3:]))


def test_typed_command_goes_through_the_language_model(settings):
    ollama = FakeOllama(responder=lambda text, schema: {"actions": [], "reply": "Paris is the capital of France."}
                        if "facts" not in schema.get("properties", {}) else {"facts": []})
    h = Harness(settings, ollama=ollama)
    with h.client:
        h.setup()
        assert h.post("/api/command", {"text": "capital of France?"}).status_code == 403  # not verified yet
        h.verify()
        r = h.post("/api/command", {"text": "What is the capital of France?"})
        assert r.status_code == 200 and r.json()["reply"] == "Paris is the capital of France."
        assert ollama.requests and ollama.requests[-1]["text"] == "What is the capital of France?"


def test_stranger_is_denied_and_logged(h):
    h.setup()
    h.verify()
    h.scene.person = "stranger"
    h.tick_until(lambda: h.svc.effective_state() == "denied")
    assert h.svc.trust().level == 0
    assert h.post("/api/command", {"text": "open safari"}).status_code == 403
    assert any(e["kind"] == "unknown_face" for e in h.svc.db.security_events(20))
    # a stranger's voice command is refused even though it names the assistant
    h.scene.say("Jarvis, delete all my notes", speaker="stranger")
    h.wait_for(lambda: any("Authentication required" in s for s in h.scene.said))


def test_photo_of_the_owner_never_unlocks(h):
    h.setup()
    h.scene.person = "photo"
    h.tick(240)
    assert h.svc.effective_state() != "approved"
    assert h.svc.trust().level == 0


def test_frozen_camera_feed_is_treated_as_a_spoof(h):
    h.setup()
    h.verify()
    h.scene.frozen = True
    h.tick_until(lambda: h.svc.effective_state() == "spoof")
    assert any(e["kind"] == "camera_frozen" for e in h.svc.db.security_events(20))


def test_voice_mismatch_blocks_commands_from_another_voice(h):
    h.setup()
    h.verify()
    h.scene.say("Jarvis, open Safari please right now", speaker="stranger")
    h.wait_for(lambda: any("doesn't match my owner" in s for s in h.scene.said))
    assert not h.rig.opened_apps


def test_deletion_waits_for_a_confirmation_in_the_owners_voice(settings):
    ollama = FakeOllama(script={
        "note buy milk": {"actions": [{"tool": "notes.add", "args": {"text": "Buy milk and bread"}}], "reply": ""},
        "delete the note": {"actions": [{"tool": "notes.delete", "args": {"query": "milk"}}], "reply": ""},
    })
    h = Harness(settings, ollama=ollama)
    milk = lambda: [n for n in h.svc.tools.store.notes(10) if "milk" in n.text.lower()]
    with h.client:
        h.setup()
        h.verify()
        h.voice_verified()
        h.scene.say("Jarvis, note buy milk and bread")
        h.wait_for(lambda: bool(milk()))
        h.scene.say("Jarvis, delete the note about milk")
        h.wait_for(lambda: h.svc.pending() is not None)
        assert milk()  # nothing is deleted before the confirmation
        # a short "yes" can't identify the speaker; a clear sentence in the owner's voice can
        h.scene.say("Yes, go ahead and delete it", duration_s=2.0)
        h.wait_for(lambda: not milk())
        h.wait_for(lambda: any(e["kind"] == "sensitive_action" for e in h.svc.db.security_events(20)))


def test_a_stranger_cannot_confirm_a_pending_deletion(settings):
    ollama = FakeOllama(script={
        "delete the note": {"actions": [{"tool": "notes.delete", "args": {"query": "milk"}}], "reply": ""},
    })
    h = Harness(settings, ollama=ollama)
    with h.client:
        h.setup()
        h.verify()
        h.voice_verified()
        h.svc.tools.store.add_note("Buy milk")
        h.scene.say("Jarvis, delete the note about milk")
        h.wait_for(lambda: h.svc.pending() is not None)
        h.scene.say("Yes, go ahead and delete it", speaker="stranger", duration_s=2.0)
        h.wait_for(lambda: any("doesn't match my owner" in s for s in h.scene.said))
        assert any("milk" in n.text.lower() for n in h.svc.tools.store.notes(10))


def test_dev_routes_drive_the_scene(settings):
    app, rig = build(settings)
    add_dev_routes(app, "test-token")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        Harness.wait_for(SimpleNamespace(svc=app.state.svc, clock=time.monotonic),
                         lambda: app.state.svc.speech.stt.ready)  # let startup finish
        assert c.put("/api/dev/scene", json={"person": "nobody"}).status_code == 401
        r = c.put("/api/dev/scene", headers=H, json={"person": "stranger", "bystanders": 1})
        assert r.json()["person"] == "stranger" and rig.scene.bystanders == 1
        assert c.post("/api/dev/say", headers=H, json={"text": "hello"}).json()["ok"]
        assert c.get("/api/dev/scene", headers=H).json()["bystanders"] == 1


def test_quitting_while_models_load_leaves_nothing_running(settings):
    """The app can close while startup is still loading models: stop() must not fail, and
    startup must not switch the camera, microphone or worker threads back on afterwards."""
    import threading

    app, rig = build(settings)
    svc = app.state.svc
    loading, release = threading.Event(), threading.Event()
    real_load = rig.stt.load

    def slow_load():
        loading.set()
        release.wait(5)
        return real_load()

    rig.stt.load = slow_load
    starter = threading.Thread(target=svc.start, name="startup")
    starter.start()
    assert loading.wait(5)
    svc.stop()  # must not raise (e.g. joining a thread that was never started)
    release.set()
    starter.join(5)
    time.sleep(0.2)
    workers = {"face-loop", "speech-in", "speech-out", "voice-loop", "mic", "camera", "alarms", "perf"}
    alive = {t.name for t in threading.enumerate() if t.name in workers}
    assert not alive, f"still running after stop: {alive}"
    assert rig.camera.status == "off" and rig.mic.status == "off"


def test_nothing_keeps_running_after_the_app_stops(settings):
    import threading

    before = {t.ident for t in threading.enumerate()}
    h = Harness(settings)
    with h.client:
        h.wait_models()
        h.svc.speech.out.say("Hello there.")  # uses the synthesis worker
        h.wait_for(lambda: bool(h.scene.said))
    end = time.monotonic() + 5
    while time.monotonic() < end:
        left = [t.name for t in threading.enumerate() if t.ident not in before and t.is_alive()]
        if not left:
            break
        time.sleep(0.05)
    assert not left, f"threads left running: {left}"


def test_a_cancelled_voice_enrollment_is_never_saved(h):
    """The owner cancels (or deletes the profile) just as the last phrase is being processed."""
    h.wait_models()
    h.post("/api/setup/profile", {"owner_name": "Aditya", "assistant_name": "Jarvis"})
    assert h.post("/api/enroll/voice/start").status_code == 200
    v = h.svc.voice
    session = v.enrollment
    real_offer = session.offer

    def offer_then_cancel(*a, **k):
        ok = real_offer(*a, **k)
        if session.done:
            v.cancel_enrollment()  # arrives from the API thread at this very moment
        return ok

    session.offer = offer_then_cancel
    for phrase in list(session.items):
        before = session.index
        h.scene.say(phrase.text, duration_s=2.4)
        h.wait_for(lambda before=before: session.index > before or v.enrollment is None)
    time.sleep(0.3)
    assert not h.svc.voice_enrolled


def test_a_cancelled_face_enrollment_is_never_saved(h):
    h.wait_models()
    h.post("/api/setup/profile", {"owner_name": "Aditya", "assistant_name": "Jarvis"})
    assert h.post("/api/enroll/face/start").status_code == 200
    session = h.svc.enrollment
    real_update = session.update

    def update_then_cancel(*a, **k):
        out = real_update(*a, **k)
        if session.done:
            h.svc.cancel_enrollment()
        return out

    session.update = update_then_cancel
    h.tick_until(lambda: session.done)
    h.tick(3)
    assert not h.svc.face_enrolled


def test_face_verification_starts_before_the_slow_models_finish_loading(settings):
    """Unlocking needs only the face model and the camera: it must not wait for speech,
    voice or the language model (Ollama can take many seconds to load a model)."""
    import threading

    import numpy as np

    from jarvis.fakes import FACES

    app, rig = build(settings)
    svc = app.state.svc
    svc.store.save("face", np.stack([FACES["owner"]] * 8))
    svc.db.set("owner_name", "Aditya")
    svc.db.set("setup_complete", "1")
    warming, release = threading.Event(), threading.Event()
    rig.ollama.warm = lambda model: (warming.set(), release.wait(10))
    starter = threading.Thread(target=svc.start, name="startup")
    starter.start()
    try:
        assert warming.wait(5)
        assert svc.mode == "verifying"
        assert svc._thread is not None and svc._thread.is_alive(), "face loop not running yet"
    finally:
        release.set()
        starter.join(10)
        svc.stop()


def test_the_first_hindi_command_is_instant(settings):
    """Transliteration tables load during startup, not on the owner's first Hindi command."""
    import subprocess
    import sys

    code = ("import sys, tempfile, pathlib; from jarvis.config import Settings; from jarvis.headless import build\n"
            "app, rig = build(Settings(data_dir=pathlib.Path(tempfile.mkdtemp()) / 'd'))\n"
            "assert 'indic_transliteration' not in sys.modules\n"
            "app.state.svc.start(); app.state.svc.stop()\n"
            "print('indic_transliteration' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.stdout.strip().endswith("True"), out.stderr[-500:]
