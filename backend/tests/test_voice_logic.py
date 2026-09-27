import sqlite3

import numpy as np

from jarvis.auth.voice.enrollment import MIN_SPEECH_S, VoiceEnrollmentSession, phrases
from jarvis.auth.voice.quality import audio_quality
from jarvis.auth.voice.vad import FRAME, Segmenter
from jarvis.auth.voice.verification import VoiceAuth
from jarvis.database.db import Database


def _run(seg, probs, amp=0.1):
    out = []
    for p in probs:
        u = seg.push(np.full(FRAME, amp if p > 0.5 else 0.001, np.float32), p)
        if u:
            out.append(u)
    return out


def test_segmenter_cuts_one_utterance_with_preroll():
    seg = Segmenter()
    probs = [0.0] * 20 + [0.9] * 50 + [0.0] * 30  # 1.6 s of speech then silence
    [u] = _run(seg, probs)
    assert abs(u.speech_s - 50 * FRAME / 16000) < 0.1
    assert len(u.audio) > 50 * FRAME  # includes pre-roll
    assert u.noise_rms < 0.01


def test_segmenter_ignores_short_blips_and_splits_on_pauses():
    assert _run(Segmenter(), [0.0] * 10 + [0.9] * 5 + [0.0] * 30) == []  # 0.16 s click
    utts = _run(Segmenter(), ([0.9] * 40 + [0.0] * 25) * 2)
    assert len(utts) == 2


def test_segmenter_caps_length():
    utts = _run(Segmenter(max_s=3.0), [0.9] * 400)
    assert utts and all(len(u.audio) / 16000 <= 3.1 for u in utts)


def test_audio_quality_prefers_clear_speech():
    rng = np.random.default_rng(0)
    speech = 0.05 * np.sin(np.linspace(0, 2000, 32000)).astype(np.float32)
    good = audio_quality(speech, 2.0, noise_rms=0.002)["score"]
    noisy = audio_quality(speech, 2.0, noise_rms=0.03)["score"]
    short = audio_quality(speech[:8000], 0.5, noise_rms=0.002)["score"]
    clipped = audio_quality(np.clip(speech * 40, -1, 1), 2.0, noise_rms=0.002)["score"]
    assert good > 0.8 and max(noisy, short, clipped) < 0.3


def _unit(v):
    return (v / np.linalg.norm(v)).astype(np.float32)


def test_phrases_use_names_and_cover_three_languages():
    ps = phrases("FRIDAY", "Aditya")
    assert {p.lang for p in ps} == {"English", "Hindi", "Hinglish"}
    assert any("FRIDAY" in p.text for p in ps) and any("Aditya" in p.text for p in ps)


def test_voice_enrollment_accepts_good_consistent_samples_only():
    rng = np.random.default_rng(3)
    owner = _unit(rng.normal(size=192))
    s = VoiceEnrollmentSession(phrases("J", "A"))

    def sample():
        return [_unit(owner + 0.02 * rng.normal(size=192)) for _ in range(3)]

    assert not s.offer(sample(), MIN_SPEECH_S - 0.2, 0.9)  # too short
    assert not s.offer(sample(), 2.0, 0.1)  # too noisy
    assert s.offer(sample(), 2.0, 0.9)
    # a different person halfway through is refused
    assert not s.offer([_unit(rng.normal(size=192))] * 3, 2.0, 0.9)
    assert "earlier recordings" in s.hint
    while not s.done:
        assert s.offer(sample(), 2.0, 0.9)
    assert s.template().shape == (len(s.items) * 3, 192)
    assert s.snapshot()["done"]


def test_voice_auth_verdicts_and_expiry():
    a = VoiceAuth(threshold=0.5, reject_threshold=0.3, valid_s=20)
    assert a.state(0) == "idle"
    assert a.judge(0.7, 0.9, 0).verdict == "verified"
    assert a.state(10) == "verified" and a.state(25) == "idle"
    assert a.judge(0.1, 0.9, 30).verdict == "rejected"
    assert a.judge(0.4, 0.9, 31).verdict == "uncertain"
    assert a.judge(0.1, 0.1, 32).verdict == "uncertain"  # noisy audio never rejects


def test_phase1_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE security_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
        "kind TEXT NOT NULL, detail TEXT NOT NULL, face_conf REAL, blocked INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    conn.close()
    db = Database(path)
    db.add_security_event("unknown_voice", "x", voice_conf=0.1)
    assert db.security_events()[0]["voice_conf"] == 0.1
