"""Fusion classifier, auth level policy and their inputs (no models or camera needed)."""

import json
from datetime import datetime, timedelta

import numpy as np
import pytest

from jarvis.auth.fusion.features import FEATURES, Evidence, vector
from jarvis.auth.fusion.model import DEFAULT_PATH, FusionModel, expand, load_model
from jarvis.auth.fusion.samples import SampleStore
from jarvis.auth.fusion.synth import SCENARIOS, generate
from jarvis.auth.fusion.train import evaluate, fit_logreg, roc_auc, train
from jarvis.auth.levels import LevelConfig, assess
from jarvis.auth.liveness.gate import LivenessConfig, LivenessGate
from jarvis.auth.voice.verification import VoiceAuth
from jarvis.database.db import Database
from jarvis.llm.intents import Action
from jarvis.security.crypto import StaticKeyProvider
from jarvis.speech_service import confirm_answer
from jarvis.tools.runner import LEVELS, Plan, ToolRunner
from jarvis.tools.store import ToolStore


# -- features -------------------------------------------------------------------------
def owner(**kw) -> Evidence:
    base = dict(face_state="approved", liveness="passed", face_sim=0.65, face_age_s=0.5, face_quality=0.8,
                live_score=0.85, live_age_s=60, voice_sim=0.66, voice_age_s=5, voice_verified_age_s=5)
    return Evidence(**{**base, **kw})


def test_vector_layout_and_defaults():
    x = vector(owner())
    assert len(x) == len(FEATURES)
    f = dict(zip(FEATURES, x))
    assert f["face_sim"] == 0.65 and f["voice_present"] == 1 and f["voice_sim"] == 0.66
    assert 0.9 < f["voice_fresh"] < 1 and 0.9 < f["live_fresh"] < 1
    # voice results older than the window are ignored entirely
    old = dict(zip(FEATURES, vector(owner(voice_age_s=90))))
    assert old["voice_present"] == old["voice_sim"] == old["voice_fresh"] == 0
    # liveness not passed -> no freshness; disabled -> neutral; missing passive model -> neutral score
    assert dict(zip(FEATURES, vector(owner(liveness="challenge"))))["live_fresh"] == 0
    assert dict(zip(FEATURES, vector(owner(liveness="disabled"))))["live_fresh"] == 0.5
    assert dict(zip(FEATURES, vector(owner(live_score=None))))["live_score"] == 0.5


# -- training ---------------------------------------------------------------------------
def test_synthetic_data_is_deterministic_and_labelled():
    X1, y1, s1 = generate(2000, seed=3)
    X2, y2, _ = generate(2000, seed=3)
    assert np.array_equal(X1, X2) and np.array_equal(y1, y2)
    assert X1.shape[1] == len(FEATURES)
    for name, (_, label, _) in SCENARIOS.items():
        assert (y1[s1 == name] == label).all()


def test_roc_auc():
    y = np.array([0, 0, 1, 1])
    assert roc_auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert roc_auc(y, np.array([0.9, 0.8, 0.2, 0.1])) == 0.0
    assert roc_auc(y, np.array([0.5, 0.5, 0.5, 0.5])) == 0.5


def test_logreg_learns_a_separable_problem():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(400, 2))
    y = (X[:, 0] + 0.5 * X[:, 1] > 0).astype(float)
    mean, scale, coef, b = fit_logreg(X, y, l2=0.01)
    p = 1 / (1 + np.exp(-(((X - mean) / scale) @ coef + b)))
    assert ((p > 0.5) == y).mean() > 0.97


def test_trained_model_quality_and_roundtrip(tmp_path):
    m = train(n=6000)
    rep = m.meta["report"]["test_simulated"]
    assert rep["auc"] > 0.98 and rep["accuracy"] > 0.95
    assert rep["scenarios"]["stranger"]["level1"] == 0.0
    assert rep["scenarios"]["owner_speaking"]["level2"] > 0.9
    m.save(tmp_path / "m.json")
    m2 = FusionModel.load(tmp_path / "m.json")
    x = vector(owner())
    assert m2.prob(x) == pytest.approx(m.prob(x), abs=1e-6)
    assert m2.prob(np.stack([x, x])).shape == (2,)


def test_shipped_model_separates_owner_from_attacks():
    m, source = load_model(None)
    assert source == "default" and m is not None
    assert m.prob(vector(owner())) > 0.95
    assert m.prob(vector(owner(voice_age_s=None, voice_sim=None))) > 0.9  # silent owner still reads
    stranger = owner(face_sim=0.05, face_age_s=40, liveness="idle", live_age_s=None, voice_sim=None)
    assert m.prob(vector(stranger)) < 0.1
    photo = owner(live_score=0.12, liveness="idle", live_age_s=None, voice_sim=None)
    assert m.prob(vector(photo)) < 0.5
    other_voice = owner(voice_sim=0.12)  # owner at the screen, someone else talking
    assert m.prob(vector(other_voice)) < 0.5
    assert json.loads(DEFAULT_PATH.read_text())["type"] == "logistic_regression_poly2"


def test_expand_adds_pairwise_terms():
    n = len(FEATURES)
    assert expand(np.ones(n)).shape == (1, n + n * (n + 1) // 2)


def test_personal_model_is_preferred_and_corrupt_one_ignored(tmp_path):
    p = tmp_path / "fusion.json"
    train(n=2000).save(p)
    assert load_model(p)[1] == "personal"
    p.write_text("{not json")
    assert load_model(p)[1] == "default"


def test_personal_samples_shift_the_model():
    X, y = generate(400, seed=5)[:2]
    m = train(X, y, n=4000)
    assert m.meta["samples"]["device_owner"] == int((y == 1).sum())
    assert "device_samples" in m.meta["report"]


def test_sample_store_throttles_and_caps(tmp_path):
    st = SampleStore(Database(tmp_path / "db.sqlite"))
    x = vector(owner())
    assert st.add(x, 1, "cmd", now=0)
    assert not st.add(x, 1, "cmd", now=10)  # near-duplicate
    assert st.add(x, 0, "stranger", now=10)
    assert st.add(x, 1, "cmd", now=40)
    assert st.counts() == {"owner": 2, "other": 1}
    X, y = st.load()
    assert X.shape == (3, len(FEATURES)) and sorted(y) == [0, 1, 1]
    st.clear()
    assert st.counts() == {"owner": 0, "other": 0}


# -- level policy --------------------------------------------------------------------------
CFG = LevelConfig()


def test_levels_ladder():
    t = assess(owner(), 0.99, CFG)
    assert t.level == 2 and t.l3_ready and t.voice_window_s == 55
    t = assess(owner(voice_verified_age_s=None, voice_age_s=None, voice_sim=None), 0.97, CFG)
    assert t.level == 1 and t.blockers[2] == "voice_needed"


@pytest.mark.parametrize("state", ["liveness", "spoof", "denied", "absent", "scanning"])
def test_not_approved_is_level0(state):
    t = assess(owner(face_state=state), 0.99, CFG)
    assert t.level == 0 and t.blockers[1] == state


def test_fusion_can_only_lower_the_level():
    assert assess(owner(), 0.3, CFG).level == 0
    assert assess(owner(), 0.7, CFG).level == 1
    t = assess(owner(), 0.85, CFG)
    assert t.level == 2 and not t.l3_ready and t.blockers[3] == "low_confidence"
    assert assess(owner(face_state="denied"), 1.0, CFG).level == 0
    assert assess(owner(), None, CFG).level == 2  # model unavailable -> rules only


def test_rejected_voice_and_bystander_cap_at_read():
    assert assess(owner(voice_rejected_since=True), 0.99, CFG).blockers[2] == "voice_rejected"
    assert assess(owner(bystander=True, others=1), 0.99, CFG).blockers[2] == "bystander"


def test_no_voice_profile_allows_level2_on_face_and_liveness():
    ev = owner(voice_enrolled=False, voice_verified_age_s=None, voice_age_s=None, voice_sim=None)
    assert assess(ev, 0.95, CFG).level == 2


def test_level3_needs_recent_liveness():
    t = assess(owner(live_age_s=900), 0.99, CFG)
    assert t.level == 2 and t.needs_fresh_liveness and not t.l3_ready
    assert assess(owner(liveness="disabled", live_age_s=None), 0.99, CFG).l3_ready


# -- inputs from the pipelines ------------------------------------------------------------
def test_voice_auth_tracks_last_owner_match_and_later_rejections():
    a = VoiceAuth(threshold=0.5, reject_threshold=0.3)
    a.judge(0.7, 0.9, now=1)
    assert a.last_verified.t == 1 and not a.rejected_since_verified
    a.judge(0.4, 0.9, now=2)  # uncertain: keeps the owner's match
    assert a.last_verified.t == 1 and not a.rejected_since_verified
    a.judge(0.1, 0.9, now=3)
    assert a.rejected_since_verified
    a.judge(0.8, 0.9, now=4)
    assert a.last_verified.t == 4 and not a.rejected_since_verified
    a.reset()
    assert a.last_verified is None


def test_liveness_demand_only_from_passed():
    g = LivenessGate(LivenessConfig())
    assert g.demand(0, "x") == []
    g.state = "passed"
    ev = g.demand(0, "Fresh check")
    assert ev == [("liveness_challenge", "Fresh check")] and g.state == "challenge"


@pytest.mark.parametrize("text,answer", [
    ("Yes, go ahead.", True), ("haan kar do", True), ("हाँ, कर दो", True), ("confirm", True),
    ("No, don't.", False), ("cancel that", False), ("nahi", False), ("नहीं", False),
    ("What time is it?", None), ("", None),
])
def test_confirm_answer(text, answer):
    assert confirm_answer(text) is answer


# -- tools ------------------------------------------------------------------------------------
@pytest.fixture
def runner(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    store = ToolStore(db, StaticKeyProvider())
    now = datetime(2026, 9, 27, 10, 0)
    return ToolRunner(db, store, None, None, None, clock=lambda: now), store, now


def test_every_tool_has_a_level():
    from jarvis.llm.intents import TOOLS

    assert set(LEVELS) == set(TOOLS)
    assert LEVELS["notes.delete"] == LEVELS["calendar.delete"] == 3


def test_plan_calendar_delete_picks_the_matching_event(runner):
    r, store, now = runner
    store.add_event("Dentist", now + timedelta(days=1, hours=5), now + timedelta(days=1, hours=6))
    store.add_event("Team meeting", now + timedelta(hours=7), now + timedelta(hours=8))
    plan = r.plan(Action("calendar.delete", {"title": "team meeting"}), "en")
    assert isinstance(plan, Plan) and plan.what == "“Team meeting” today at 5:00 PM"
    assert r.execute(plan).ok and [e.title for e in store.events_between(now, now + timedelta(days=3))] == ["Dentist"]
    assert not r.execute(plan).ok  # already gone
    assert not r.plan(Action("calendar.delete", {"title": "yoga"}), "en").ok


def test_alarm_cancel(runner):
    r, store, now = runner
    store.add_alarm("alarm", now + timedelta(hours=1))
    store.add_alarm("alarm", now + timedelta(hours=2))
    res = r.run(Action("alarm.cancel", {"time": (now + timedelta(hours=1)).isoformat(timespec="minutes")}), "en")
    assert res.ok and res.say == "Cancelled the 11:00 AM alarm."
    assert len(store.alarms()) == 1
    assert r.run(Action("alarm.cancel", {}), "en").say == "Cancelled the 12:00 PM alarm."
    assert not r.run(Action("alarm.cancel", {}), "en").ok


def test_someone_else_talking_does_not_lock_the_owner_out_of_reading():
    from jarvis.auth.fusion.features import without_voice

    m, _ = load_model(None)
    ev = owner(voice_sim=0.1, voice_rejected_since=True)
    x = vector(ev)
    prob, presence = m.prob(x), m.prob(without_voice(x))
    assert prob < 0.5 < 0.9 < presence
    t = assess(ev, prob, CFG, presence)
    assert t.level == 1 and t.blockers[2] == "voice_rejected"
