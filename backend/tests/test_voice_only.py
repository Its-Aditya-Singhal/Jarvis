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
        # typed commands are off by default (the keyboard would bypass the voice match)...
        assert svc.prefs.get("security.typed") == "off" and svc.trust(screen=True).level == 1
        assert client.post("/api/enroll/voice/start", headers=H).status_code == 403
        # ...and when switched on in Settings they stand in for the voice
        svc.prefs.set("security.typed", "on")
        assert svc.trust(screen=True).level == 2
        assert client.post("/api/enroll/voice/start", headers=H).status_code == 200
        client.post("/api/enroll/voice/cancel", headers=H)
        svc.prefs.set("security.typed", "off")
        svc.voice.auth.judge(0.9, 0.9, time.monotonic(), 2.0)
        t = svc.trust()
        assert t.level == 2 and t.l3_ready  # no liveness to wait for
        assert client.post("/api/enroll/voice/start", headers=H).status_code == 200
        # someone else speaking blocks level 2 until the owner speaks again
        svc.voice.auth.judge(0.1, 0.9, time.monotonic(), 2.0)
        assert svc.trust().level == 1


def test_the_mac_password_stands_in_for_the_voice_in_settings(tmp_path):
    """The stored voiceprint scores the owner as someone else (or nothing was said yet): macOS's
    password prompt unlocks Settings for two minutes, so the voice can be re-recorded and a key
    saved. Commands still need the voice."""
    from jarvis.security.presence import confirm_mac_user

    client, svc = _client(_voice_only(tmp_path))
    with client:
        _voice_ready(svc)
        client.post("/api/setup/profile", headers=H, json={"owner_name": "Aditya", "assistant_name": "JARVIS"})
        _enroll_voice(svc)
        client.post("/api/setup/complete", headers=H)
        svc.voice.auth.judge(0.01, 0.9, time.monotonic(), 2.0)  # "Jarvis, hello" scored as a stranger
        asked: list[str] = []
        svc.prove_presence = lambda reason: asked.append(reason) or False  # cancelled / wrong password
        r = client.post("/api/enroll/voice/start", headers=H)
        assert r.status_code == 403 and "Mac password" in r.json()["detail"]
        r = client.put("/api/settings/google", headers=H, json={"services": ["gmail"]})
        assert r.status_code == 403 and "Mac password" in r.json()["detail"]
        r = client.post("/api/unlock", headers=H)
        assert r.status_code == 403 and "Mac password" in r.json()["detail"]
        assert client.post("/api/enroll/voice/start", headers=H).status_code == 403
        assert client.get("/api/status", headers=H).json()["auth"]["mac_unlock_s"] == 0
        svc.prove_presence = lambda reason: asked.append(reason) or True
        r = client.post("/api/unlock", headers=H)
        assert r.status_code == 200 and r.json()["seconds"] == 120
        assert "two minutes" in asked[-1]
        assert client.get("/api/status", headers=H).json()["auth"]["mac_unlock_s"] > 100
        assert client.post("/api/enroll/voice/start", headers=H).status_code == 200
        client.post("/api/enroll/voice/cancel", headers=H)
        assert client.put("/api/settings/google", headers=H, json={"services": ["gmail"]}).status_code == 200
        # a spoken command still needs the voice: the password only covers clicks in Settings
        assert svc.trust().level == 1
        # and the window runs out
        later = time.monotonic() + 1000
        svc.clock = lambda: later
        assert svc.mac_unlocked_s() == 0
        assert client.put("/api/settings/google", headers=H, json={"services": ["gmail"]}).status_code == 403
        events = [e["kind"] for e in svc.db.security_events(50)]
        assert "mac_unlock" in events and "mac_unlock_failed" in events

    # the prompt itself: macOS only, the script runs /usr/bin/true, and only exit 0 counts
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return type("R", (), {"returncode": 0 if "allow" in argv[-1] else 1})()

    assert confirm_mac_user('allow "it"', run=run, platform="darwin")
    assert calls[0][0] == "/usr/bin/osascript" and 'do shell script "/usr/bin/true"' in calls[0][2]
    assert "\"it\"" not in calls[0][2]  # quotes in the reason can't break out of the script
    assert not confirm_mac_user("deny", run=run, platform="darwin")
    assert not confirm_mac_user("allow", run=run, platform="linux") and len(calls) == 2


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


def test_voice_only_never_downloads_the_face_models(tmp_path):
    from jarvis.downloads import ModelDownloader

    ids = [p.id for p in ModelDownloader(tmp_path, skip=("face", "liveness")).packs]
    assert "face" not in ids and "liveness" not in ids and "voice" in ids
    assert "face" in [p.id for p in ModelDownloader(tmp_path).packs]


def test_strict_voice_match_is_the_default(tmp_path):
    from jarvis.prefs import VOICE_PRESETS

    client, svc = _client(_voice_only(tmp_path))
    with client:
        assert svc.prefs.get("security.voice") == "strict"
        assert (svc.s.voice_threshold, svc.s.voice_reject_threshold) == VOICE_PRESETS["strict"]


def test_level_two_needs_this_utterance_verified(tmp_path):
    """A voice match a minute ago isn't enough: the request itself must be in the owner's voice."""
    from jarvis.llm.intents import Action

    client, svc = _client(_voice_only(tmp_path), tools=True)
    with client:
        _voice_ready(svc)
        client.post("/api/setup/profile", headers=H, json={"owner_name": "Aditya", "assistant_name": "JARVIS"})
        _enroll_voice(svc)
        client.post("/api/setup/complete", headers=H)
        svc.voice.auth.judge(0.9, 0.9, time.monotonic(), 2.0)  # verified a moment ago
        note = Action("notes.add", {"text": "buy milk"}, "")
        r = svc._run_tools([note], "en", "voice", verdict="uncertain")
        assert not r[0].ok and r[0].data["blocked"] == "voice_needed" and "confirm your voice" in r[0].say
        assert svc._run_tools([note], "en", "voice", verdict="verified")[0].ok
        # a delay can't smuggle a level-2 action past an unconfirmed voice either
        wait = Action("wait", {"seconds": 5}, "")
        r = svc._run_tools([wait, Action("app.open", {"name": "Safari"}, "")], "en", "voice", verdict="uncertain")
        assert not r[0].ok and svc.delayed() == []


def test_short_clips_are_never_stitched_together(tmp_path):
    """"JARVIS" … "louder": each is too short to judge, and two of them are not joined into one clip
    (the owner's name plus someone else's short "read my mail" could pass as the owner)."""
    from test_api import FakeMic

    from jarvis.auth.matching import TemplateMatcher
    from jarvis.auth.voice.vad import Utterance
    from jarvis.database.db import Database
    from jarvis.events import EventBus
    from jarvis.security.template_store import TemplateStore
    from jarvis.voice_service import VoiceService

    class Speaker:
        ready = True
        lengths: list = []

        def embed(self, audio):
            Speaker.lengths.append(len(audio))
            return np.eye(1, 192, dtype=np.float32)[0]

    s = _voice_only(tmp_path)
    db = Database(tmp_path / "v.sqlite3")
    v = VoiceService(s, db, TemplateStore(tmp_path / "t", StaticKeyProvider()), EventBus(), Speaker(), FakeMic(),
                     lambda: True, lambda: ("JARVIS", "Aditya"))
    matcher = TemplateMatcher(np.tile(np.eye(1, 192, dtype=np.float32), (6, 1)), top_k=5)
    q = {"score": 0.9}
    class Speech:
        submitted: list = []

        def submit(self, audio, verdict):
            Speech.submitted.append(verdict)

    v.speech = Speech()
    v.matcher = matcher
    name = Utterance(np.zeros(8000, np.float32), 0.5, 0.0)
    louder = Utterance(np.zeros(8000, np.float32), 0.5, 0.0)
    v._verify_utterance(name, q)
    v._verify_utterance(louder, q)
    assert [f() for f in Speech.submitted] == ["uncertain", "uncertain"]
    assert Speaker.lengths == []  # the speaker model never ran on them
    db.close()


def test_a_refused_keychain_never_loses_the_saved_voice(tmp_path):
    """A rebuilt app that macOS doesn't trust yet can't read the key: the voice file stays as it is,
    nothing new is sealed over it, and the health list says how to let JARVIS in again."""
    from keyring.errors import KeyringError

    s, keys = _voice_only(tmp_path), StaticKeyProvider()
    client, svc = _client(s, keys=keys)
    with client:
        _voice_ready(svc)
        client.post("/api/setup/profile", headers=H, json={"owner_name": "Aditya", "assistant_name": "JARVIS"})
        _enroll_voice(svc)
        client.post("/api/setup/complete", headers=H)
    tmpl = s.templates_dir / "voice.tmpl"
    before = tmpl.read_bytes()

    class Refused(StaticKeyProvider):
        def get_key(self):
            raise KeyringError("Can't get password from keychain: (-25293, 'Security Auth Failure')")

    client, svc = _client(s, keys=Refused())
    with client:
        _voice_ready(svc)
        svc.voice.begin_verification()
        assert svc.voice.profile_error == "keychain" and svc.voice.matcher is None
        (issue,) = [i for i in svc.issues() if i["id"] == "voice_profile"]
        assert "Always Allow" in issue["fix"] and "still saved" in issue["fix"]
    assert tmpl.read_bytes() == before
    client, svc = _client(s, keys=keys)  # macOS lets it in again: the same voice works
    with client:
        _voice_ready(svc)
        assert svc.voice.begin_verification() and svc.voice.profile_error is None
