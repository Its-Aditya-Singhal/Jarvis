import numpy as np

from jarvis.auth.face.continuous import ContinuousFaceAuth, FrameResult
from jarvis.auth.face.enrollment import STEPS, EnrollmentSession
from jarvis.auth.face.quality import quality_score
from jarvis.auth.face.types import FaceObservation
from jarvis.auth.matching import TemplateMatcher, confidence


def _unit(v):
    return v / np.linalg.norm(v)


def test_quality_prefers_large_sharp_well_lit_faces():
    good = quality_score(0.9, 200, 300, 125)
    small = quality_score(0.9, 30, 300, 125)
    blurry = quality_score(0.9, 200, 2, 125)
    dark = quality_score(0.9, 200, 300, 5)
    assert good > 0.9
    assert max(small, blurry, dark) < 0.2


def test_verifier_topk_similarity():
    rng = np.random.default_rng(1)
    owner = _unit(rng.normal(size=512))
    template = np.stack([_unit(owner + 0.3 * rng.normal(size=512) / 22) for _ in range(20)])
    v = TemplateMatcher(template, top_k=5)
    assert v.similarity(owner) > 0.9
    assert abs(v.similarity(_unit(rng.normal(size=512)))) < 0.2
    assert v.self_consistency() > 0.8
    assert confidence(0.42, 0.42) == 0.5


def _auth():
    return ContinuousFaceAuth(threshold=0.42, reject_threshold=0.25, window=5, reauth_interval_s=30, absence_lock_s=8)


def _feed(auth, sims, t0, n, dt=0.2):
    events = []
    for i in range(n):
        events += auth.update(FrameResult(similarities=sims), t0 + i * dt)
    return events


def test_owner_is_approved_after_a_few_frames_not_one():
    a = _auth()
    a.update(FrameResult([0.7]), 0.0)
    assert a.state == "scanning"  # a single frame is never enough
    events = _feed(a, [0.7], 0.2, 3)
    assert a.state == "approved"
    assert ("state_approved", "Owner verified") in events


def test_stranger_is_denied_and_does_not_see_confidence():
    a = _auth()
    events = _feed(a, [0.05], 0.0, 4)
    assert a.state == "denied"
    assert any(k == "state_denied" for k, _ in events)
    assert a.snapshot().confidence_sim is None


def test_stranger_replacing_owner_revokes_approval():
    a = _auth()
    _feed(a, [0.7], 0.0, 4)
    assert a.state == "approved"
    _feed(a, [0.05], 1.0, 5)
    assert a.state == "denied"


def test_approval_expires_without_fresh_evidence_and_locks_on_absence():
    a = _auth()
    _feed(a, [0.7], 0.0, 4)
    # owner walks away
    _feed(a, [], 1.0, 50)  # 10 s of empty frames
    assert a.state == "absent"
    # re-verification needed again
    a2 = _auth()
    _feed(a2, [0.7], 0.0, 4)
    _feed(a2, [0.39], 1.0, 200)  # borderline for 40 s: hysteresis holds, then expires
    assert a2.state == "scanning"


def test_bystander_flag_fires_once():
    a = _auth()
    _feed(a, [0.7], 0.0, 4)
    events = _feed(a, [0.7, 0.05], 1.0, 5)
    assert [k for k, _ in events].count("bystander") == 1
    assert a.snapshot().bystander


def _obs(yaw=0.0, pitch=0.0, rel=0.25, smile=1.0, quality=0.9):
    lm = np.zeros((68, 2), np.float32)
    lm[36], lm[45] = (0, 0), (100, 0)
    lm[48], lm[54] = (0, 50), (60 * smile, 50)
    return FaceObservation(
        bbox=np.array([0, 0, rel * 640, rel * 640], np.float32),
        det_score=0.9,
        embedding=_unit(np.random.default_rng().normal(size=512)).astype(np.float32),
        pitch=pitch, yaw=yaw, roll=0.0, landmarks=lm, quality=quality,
        frame_width=640, frame_height=480,
    )


def test_enrollment_requires_each_pose_and_rejects_wrong_direction():
    s = EnrollmentSession(samples_per_step=2, min_interval_s=0.0)
    t = 0.0

    def feed(o, n=2):
        nonlocal t
        for _ in range(n):
            t += 1
            s.update([o], t)

    # wrong pose for "straight" is not captured
    feed(_obs(yaw=30))
    assert s.step_index == 0
    feed(_obs())                        # straight
    feed(_obs(yaw=-20))                 # left (defines sign)
    feed(_obs(yaw=-20))                 # "right" in the SAME direction -> rejected
    assert STEPS[s.step_index].key == "right" and s.hint == "Other way"
    feed(_obs(yaw=20))                  # right
    feed(_obs(pitch=12))                # up
    feed(_obs(pitch=-12))               # down
    feed(_obs(rel=0.35))                # closer
    feed(_obs(rel=0.18))                # farther
    feed(_obs(smile=1.2))               # smile
    feed(_obs())                        # neutral
    assert s.done and s.progress == 1.0
    assert s.template().shape == (len(STEPS) * 2, 512)


def test_enrollment_rejects_multiple_faces_and_low_quality():
    s = EnrollmentSession(samples_per_step=1, min_interval_s=0.0)
    assert not s.update([_obs(), _obs()], 1)
    assert not s.update([_obs(quality=0.1)], 2)
    assert s.update([_obs()], 3)


def test_stranger_in_the_owners_seat_is_never_approved_by_the_owners_old_frames():
    a = _auth()
    _feed(a, [0.7], 0.0, 5)
    renewed = a.last_verified_t
    states = []
    for i in range(3):
        a.update(FrameResult([0.05]), 2.0 + i * 0.2)
        states.append(a.state)
        assert a.last_verified_t == renewed  # the stranger's frames never renew the approval
    assert states[1:] == ["scanning", "scanning"]  # two clearly foreign frames end it


def test_one_blurred_frame_keeps_the_owner_approved():
    a = _auth()
    _feed(a, [0.7], 0.0, 5)
    a.update(FrameResult([0.1]), 1.2)
    assert a.state == "approved"
    a.update(FrameResult([0.7]), 1.4)
    assert a.state == "approved" and a.last_verified_t == 1.4


def test_approval_needs_the_owner_in_the_current_frame():
    a = _auth()
    _feed(a, [0.7], 0.0, 2)
    a.update(FrameResult([0.3]), 0.5)  # median [0.7, 0.7, 0.3] is high, but this face isn't the owner
    assert a.state != "approved"


def test_a_lookalike_next_to_the_owner_counts_as_a_bystander():
    a = _auth()
    _feed(a, [0.7], 0.0, 4)
    events = _feed(a, [0.7, 0.35], 1.0, 3)  # between the reject and accept thresholds
    assert "bystander" in [k for k, _ in events] and a.snapshot().bystander
    _feed(a, [0.7], 2.0, 2)
    assert not a.snapshot().bystander
