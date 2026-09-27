"""Liveness logic with synthetic observations (no camera, no models)."""

import random

import numpy as np

from jarvis.auth.liveness.blink import BlinkDetector
from jarvis.auth.liveness.challenges import ChallengeSession, LiveObs, random_steps
from jarvis.auth.liveness.gate import LivenessConfig, LivenessGate
from jarvis.auth.liveness.replay import FrozenFeedDetector

DT = 1 / 12  # challenge frame period


def obs(yaw=0.0, width=0.25, center=(0.5, 0.5), eye=0.35, live=0.95):
    return LiveObs(yaw=yaw, rel_width=width, center=center, eye_open=eye, live_score=live)


def feed_blink(det, t, open_=0.35, closed=0.2):
    """Two open frames, two closed, one open. Returns (blinked, t)."""
    hit = False
    for v in (open_, open_, closed, closed, open_):
        hit |= det.update(v, t)
        t += DT
    return hit, t


# -- blink -------------------------------------------------------------------
def test_blink_detected_relative_to_baseline():
    det = BlinkDetector()
    t = 0.0
    for _ in range(8):
        assert not det.update(0.35, t)
        t += DT
    hit, t = feed_blink(det, t)
    assert hit


def test_slow_drift_and_long_closure_are_not_blinks():
    det = BlinkDetector()
    t = 0.0
    for v in np.linspace(0.36, 0.33, 20):  # gradual change, e.g. lighting
        assert not det.update(float(v), t)
        t += DT
    for _ in range(20):  # eyes shut for ~1.7 s
        assert not det.update(0.15, t)
        t += DT
    assert not det.update(0.35, t)


# -- challenges ----------------------------------------------------------------
def test_random_steps_always_include_blink():
    rng = random.Random(1)
    seen = set()
    for _ in range(50):
        steps = random_steps(rng, 2)
        assert "blink" in steps and len(set(steps)) == 2
        seen.add(tuple(steps))
    assert len(seen) >= 4  # order and extra action vary


def run(session, frames, t=0.0, blinks=()):
    result = "running"
    for i, o in enumerate(frames):
        result = session.update(o, t, i in blinks)
        t += DT
        if result != "running":
            break
    return result, t


def test_turn_left_requires_positive_yaw_movement():
    s = ChallengeSession(["turn_left"], 8.0, 0.0)
    result, _ = run(s, [obs(yaw=0)] + [obs(yaw=-25)] * 3)
    assert result == "running" and s.hint == "Other way"
    result, _ = run(s, [obs(yaw=y) for y in (5, 12, 20)], t=1.0)
    assert result == "passed"


def test_blink_twice_and_closer():
    s = ChallengeSession(["blink", "closer"], 8.0, 0.0)
    result, t = run(s, [obs()] * 6, blinks={1, 4})
    assert s.step == "closer"
    result, _ = run(s, [obs(width=w) for w in (0.25, 0.28, 0.31, 0.33)], t=t)
    assert result == "passed"


def test_timeout_and_face_jump_fail():
    s = ChallengeSession(["blink"], 2.0, 0.0)
    result, _ = run(s, [obs()] * 40)
    assert result == "failed" and "Timed out" in s.failure
    s = ChallengeSession(["closer"], 8.0, 0.0)
    result, _ = run(s, [obs(center=(0.3, 0.5)), obs(center=(0.7, 0.5))])
    assert result == "failed" and "changed" in s.failure


# -- gate ----------------------------------------------------------------------
def make_gate(**kw):
    cfg = LivenessConfig(**{"rechallenge_min_s": 100, "rechallenge_max_s": 100, **kw})
    return LivenessGate(cfg, rng=random.Random(0))


def pass_challenge(gate, t, live=0.95):
    """Perform whatever the gate asks. Returns t after passing."""
    assert gate.state == "challenge"
    for _ in range(200):
        step = gate.session.step
        frames = {
            "blink": [0.35, 0.35, 0.35, 0.2, 0.35, 0.35, 0.2, 0.35],
            "turn_left": [0.35] * 4,
            "turn_right": [0.35] * 4,
            "closer": [0.35] * 4,
        }[step]
        for i, eye in enumerate(frames):
            yaw = {"turn_left": 8 * i, "turn_right": -8 * i}.get(step, 0.0)
            width = 0.25 * (1 + 0.1 * i) if step == "closer" else 0.25
            gate.update("approved", obs(yaw=yaw, width=width, eye=eye, live=live), t)
            t += DT
            if gate.state != "challenge":
                return t
    raise AssertionError("challenge never finished")


def warm(gate, t, n=8, **o):
    for _ in range(n):
        gate.update("approved", obs(**o), t)
        t += DT
    return t


def test_unlock_requires_challenge_then_passes():
    g = make_gate()
    events = g.update("approved", obs(), 0.0)
    assert g.state == "challenge" and events[0][0] == "liveness_challenge"
    t = pass_challenge(g, DT)
    assert g.state == "passed"


def test_photo_is_flagged_as_spoof():
    g = make_gate()
    t = warm(g, 0.0, live=0.05)
    assert g.state == "spoof"
    # it stays blocked while the photo is held up
    t = warm(g, t, n=20, live=0.05)
    assert g.state == "spoof"


def test_low_texture_score_fails_the_challenge():
    g = make_gate(spoof_threshold=0.1)
    g.update("approved", obs(live=0.4), 0.0)
    pass_challenge(g, DT, live=0.4)
    assert g.state == "cooldown" and "texture" in g.reason


def test_ambiguous_texture_after_unlock_forces_a_new_challenge():
    g = make_gate()
    g.update("approved", obs(), 0.0)
    t = pass_challenge(g, DT)
    t = warm(g, t, n=12, live=0.35)  # e.g. a photo swapped in: not clearly fake, not live
    assert g.state == "challenge" and "Anti-spoof" in g.reason
    pass_challenge(g, t, live=0.35)  # performs the actions but texture stays ambiguous
    assert g.state == "cooldown"


def test_frozen_feed_blocks():
    g = make_gate()
    g.update("approved", obs(), 0.0)
    g.update("approved", obs(), DT, frozen=True)
    assert g.state == "spoof" and "frozen" in g.reason


def test_owner_leaving_resets_liveness():
    g = make_gate()
    g.update("approved", obs(), 0.0)
    t = pass_challenge(g, DT)
    g.update("approved", None, t + 6.0)
    assert g.state == "idle"
    g = make_gate()
    g.update("approved", obs(), 0.0)
    t = pass_challenge(g, DT)
    events = g.update("absent", None, t)
    assert g.state == "idle" and events[0][0] == "liveness_reset"


def test_repeated_failures_lock_out():
    g = make_gate(step_timeout_s=1.0, max_failures=3, retry_cooldown_s=1.0, lockout_s=30.0)
    t = 0.0
    kinds = []
    for _ in range(3):
        for _ in range(30):
            kinds += [k for k, _ in g.update("approved", obs(eye=None), t)]
            t += DT
            if g.state == "cooldown":
                break
        while g.state == "cooldown" and "locked" not in g.reason:
            g.update("approved", obs(eye=None), t)
            t += DT
    assert kinds.count("liveness_failed") == 3 and "liveness_lockout" in kinds
    g.update("approved", obs(), t + 10)
    assert g.state == "cooldown"
    g.update("approved", obs(), t + 31)
    assert g.state == "challenge"


def test_random_recheck_and_blink_gap():
    g = make_gate(blink_gap_s=40)
    g.update("approved", obs(), 0.0)
    t = pass_challenge(g, DT)
    t = warm(g, t, n=int(30 / DT), eye=None)  # 30 s without any blink
    assert g.state == "passed"
    t = warm(g, t, n=int(11 / DT), eye=None)
    assert g.state == "challenge" and "blink" in g.reason
    t = pass_challenge(g, t)
    # routine re-check at the scheduled random time (100 s here)
    for _ in range(int(101 / 0.5)):
        eye = 0.2 if int(t * 2) % 20 == 0 else 0.35  # natural blinking
        g.update("approved", obs(eye=eye), t)
        t += 0.5
        if g.state == "challenge":
            break
    assert g.state == "challenge" and "Routine" in g.reason


def test_public_hides_scores_from_non_owner():
    g = make_gate()
    g.update("approved", obs(), 0.0)
    assert "live_score" not in g.public(0.1, owner=False)
    assert g.public(0.1, owner=False)["challenge"]["step_count"] == 2
    assert "live_score" in g.public(0.1, owner=True)


# -- replay --------------------------------------------------------------------
def test_frozen_feed_detector():
    d = FrozenFeedDetector(frozen_after_s=1.0)
    rng = np.random.default_rng(0)
    base = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    for i in range(30):  # sensor noise: never frozen
        noisy = np.clip(base.astype(np.int16) + rng.integers(-2, 3, base.shape), 0, 255).astype(np.uint8)
        assert not d.update(noisy, i * DT)
    frozen = [d.update(base, 3 + i * DT) for i in range(20)]
    assert not frozen[0] and frozen[-1]
