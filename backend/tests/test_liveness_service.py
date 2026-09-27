"""Face + liveness composed in AssistantService, driven with scripted faces."""

import numpy as np

from jarvis.auth.face.types import FaceObservation
from jarvis.database.db import Database
from jarvis.events import EventBus
from jarvis.security.crypto import StaticKeyProvider
from jarvis.security.template_store import TemplateStore
from jarvis.service import AssistantService

OWNER = np.eye(1, 512, dtype=np.float32)[0]


class NoisyCamera:
    status, error = "active", None
    rng = np.random.default_rng(0)

    def start(self): ...
    def stop(self): ...
    def latest(self, max_age_s=1.0):
        return self.rng.integers(0, 255, (120, 160, 3), dtype=np.uint8)


class ScriptedEngine:
    """Returns whatever face the test put in ``self.face``."""

    ready, error = True, None

    def __init__(self):
        self.face = None

    def load(self): return True
    def analyze(self, frame):
        if self.face is None:
            return []
        return list(self.face) if isinstance(self.face, list) else [self.face]


STRANGER = np.eye(1, 512, 7, dtype=np.float32)[0]


def face(yaw=0.0, width=160.0, eye=0.35, live=0.95, emb=OWNER):
    x0 = 320 - width / 2
    return FaceObservation(
        bbox=np.array([x0, 100, x0 + width, 100 + width * 1.2], np.float32), det_score=0.9,
        embedding=emb, pitch=0.0, yaw=yaw, roll=0.0, landmarks=None, quality=0.9,
        frame_width=640, frame_height=480, eye_open=eye, live_score=live,
    )


def make_service(settings):
    store = TemplateStore(settings.templates_dir, StaticKeyProvider())
    store.save("face", np.stack([OWNER] * 3))
    db = Database(settings.db_path)
    engine = ScriptedEngine()
    svc = AssistantService(settings, db, store, EventBus(), engine, NoisyCamera())
    assert svc.begin_verification()
    return svc, engine, db


def drive(svc, engine, t, frames):
    for f in frames:
        engine.face = f
        svc.clock = lambda t=t: t  # the trust check reads the same scripted clock
        svc._tick(t)
        t += 1 / 12
    return t


def do_challenge(svc, engine, t, live=0.95):
    for _ in range(10):
        if svc.live.state != "challenge":
            break
        step = svc.live.session.step
        if step == "blink":
            seq = [face(eye=e, live=live) for e in (0.35, 0.35, 0.35, 0.2, 0.35, 0.35, 0.2, 0.35)]
        elif step == "closer":
            seq = [face(width=160 * (1 + 0.1 * i), live=live) for i in range(5)]
        else:
            sign = 1 if step == "turn_left" else -1
            seq = [face(yaw=sign * 7 * i, live=live) for i in range(5)]
        t = drive(svc, engine, t, seq)
    return t


def test_face_match_alone_does_not_unlock(settings):
    svc, engine, db = make_service(settings)
    t = drive(svc, engine, 0.0, [face()] * 6)
    assert svc.auth.state == "approved"  # identity matched…
    assert svc.effective_state() == "liveness" and not svc.owner_verified()  # …but not yet proven live
    pub = svc.auth_public()
    assert pub["liveness"]["challenge"]["prompt"] and "face_confidence" not in pub
    t = do_challenge(svc, engine, t)
    assert svc.effective_state() == "approved" and svc.owner_verified()
    assert svc.auth_public()["face_confidence"] is not None


def test_photo_of_owner_is_blocked_and_logged(settings):
    svc, engine, db = make_service(settings)
    drive(svc, engine, 0.0, [face(live=0.03)] * 12)
    assert svc.effective_state() == "spoof" and not svc.owner_verified()
    kinds = [e["kind"] for e in db.security_events(10)]
    assert "spoof_suspected" in kinds


def test_frozen_camera_is_blocked(settings):
    svc, engine, db = make_service(settings)
    still = np.zeros((120, 160, 3), np.uint8)
    svc.camera.latest = lambda max_age_s=1.0: still
    drive(svc, engine, 0.0, [face()] * 40)  # > 2 s of identical frames
    assert svc.effective_state() == "spoof"
    assert "camera_frozen" in [e["kind"] for e in db.security_events(10)]


def test_liveness_can_be_disabled(settings):
    settings.liveness_enabled = False
    svc, engine, _ = make_service(settings)
    drive(svc, engine, 0.0, [face()] * 6)
    assert svc.owner_verified()
    assert svc.status()["models"]["liveness"] == "disabled"


def test_trust_levels_follow_the_scene(settings):
    svc, engine, db = make_service(settings)
    t = drive(svc, engine, 0.0, [face()] * 6)
    assert svc.auth_public()["level"] == 0 and "trust" not in svc.auth_public()
    t = do_challenge(svc, engine, t)
    pub = svc.auth_public()
    # no voice pipeline here, so face + liveness reach level 2
    assert pub["level"] == 2 and pub["trust"]["prob"] > 0.9 and pub["trust"]["model"] == "default"
    assert set(pub["trust"]["features"]) >= {"face_sim", "live_fresh", "voice_sim"}
    # someone unknown steps into view: capped at read-only
    t = drive(svc, engine, t, [[face(), face(emb=STRANGER, width=120)]] * 4)
    tr = svc.trust()
    assert tr.level == 1 and tr.blockers[2] == "bystander"
    t = drive(svc, engine, t, [face()] * 4)
    assert svc.trust().level == 2


def test_fusion_veto_shows_as_scanning(settings):
    svc, engine, db = make_service(settings)
    do_challenge(svc, engine, drive(svc, engine, 0.0, [face()] * 6))
    svc.fusion.prob = lambda x: 0.2  # the combined evidence looks wrong
    pub = svc.auth_public()
    assert pub["state"] == "scanning" and pub["level"] == 0 and not svc.owner_verified()
    assert pub["reason"] == "Combined confidence is too low"
