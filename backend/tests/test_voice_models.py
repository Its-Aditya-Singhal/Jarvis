"""Checks with the real Silero VAD and ECAPA models on macOS synthetic voices.

Different `say` voices stand in for different speakers. They are not real
people, but they exercise the full pipeline end to end.
"""

import shutil
import subprocess
import wave

import numpy as np
import pytest

from conftest import MODELS
from jarvis.auth.matching import TemplateMatcher
from jarvis.auth.voice.engine import SpeakerEngine
from jarvis.auth.voice.vad import FRAME, Segmenter, SileroVAD

SENTENCES = [
    "Hello, please authenticate me now, this is my voice.",
    "Kal subah saat baje mujhe jagana, theek hai.",
    "Mera calendar check kar aur batao aaj kya hai.",
]
VOICES = ["Daniel", "Samantha", "Albert"]

# the synthetic speakers come from macOS `say`
pytestmark = [pytest.mark.mac, pytest.mark.models,
              pytest.mark.skipif(shutil.which("say") is None, reason="macOS `say` not available")]


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("voices")
    out = {}
    for v in VOICES:
        for i, text in enumerate(SENTENCES):
            path = d / f"{v}_{i}.wav"
            r = subprocess.run(["say", "-v", v, "-o", str(path), "--data-format=LEI16@16000", text])
            if r.returncode != 0:
                pytest.skip(f"voice {v} not installed")
            with wave.open(str(path)) as w:
                out[(v, i)] = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    return out


@pytest.fixture(scope="module")
def speaker():
    e = SpeakerEngine(MODELS)
    if not e.load():
        pytest.skip("voice model not downloaded")
    return e


def _segment(audio):
    vad, seg, out = SileroVAD(), Segmenter(), []
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        f = audio[i : i + FRAME]
        u = seg.push(f, vad(f))
        if u:
            out.append(u)
    return out


def test_vad_finds_speech_and_ignores_noise(clips):
    rng = np.random.default_rng(0)
    speech = np.concatenate([np.zeros(8000, np.float32), clips[("Daniel", 0)], np.zeros(16000, np.float32)])
    utts = _segment(speech)
    assert len(utts) >= 1 and sum(u.speech_s for u in utts) > 1.5
    noise = (0.01 * rng.normal(size=16000 * 3)).astype(np.float32)
    assert _segment(noise) == []


def test_same_voice_matches_and_other_voices_do_not(clips, speaker):
    for v in VOICES:
        windows = [w for i in (0, 1) for w in speaker.embed_windows(clips[(v, i)])]
        m = TemplateMatcher(np.stack(windows), top_k=5)
        for other in VOICES:
            sim = m.similarity(speaker.embed(clips[(other, 2)]))  # unseen sentence
            if other == v:
                assert sim > 0.5, (v, sim)
            else:
                assert sim < 0.3, (v, other, sim)


class QueueMic:
    """Plays queued clips in real 512-sample blocks, with silence in between."""

    status, error, device_name, level = "active", None, "Test mic", 0.0

    def __init__(self):
        self.blocks = []

    def play(self, audio):
        audio = np.concatenate([audio, np.zeros(16000, np.float32)])
        self.blocks += [audio[i : i + FRAME] for i in range(0, len(audio) - FRAME + 1, FRAME)]

    def start(self): ...
    def stop(self): ...
    def drain(self): ...

    def read(self, timeout=0.5):
        import time
        if not self.blocks:
            time.sleep(0.02)
            return None
        return self.blocks.pop(0)


def test_voice_service_end_to_end(clips, speaker, settings):
    import time

    from jarvis.database.db import Database
    from jarvis.events import EventBus
    from jarvis.security.crypto import StaticKeyProvider
    from jarvis.security.template_store import TemplateStore
    from jarvis.voice_service import VoiceService

    db = Database(settings.db_path)
    db.set("setup_complete", "1")
    store = TemplateStore(settings.templates_dir, StaticKeyProvider())
    mic = QueueMic()
    svc = VoiceService(settings, db, store, EventBus(), speaker, mic,
                       owner_verified=lambda: True, names=lambda: ("FRIDAY", "Aditya"))
    svc.start()
    try:
        def wait(pred, timeout=30):
            end = time.monotonic() + timeout
            while time.monotonic() < end and not pred():
                time.sleep(0.05)
            return pred()

        svc.begin_enrollment(needs_owner=True)
        for i in range(6):  # six phrases, all read by "Daniel"
            mic.play(clips[("Daniel", i % 3)])
        assert wait(lambda: svc.mode == "verifying"), svc.enrollment and svc.enrollment.snapshot()
        assert store.exists("voice")

        mic.play(clips[("Daniel", 2)])
        assert wait(lambda: svc.auth.last is not None)
        assert svc.auth.last.verdict == "verified"

        mic.play(clips[("Samantha", 0)])
        assert wait(lambda: svc.auth.last.verdict != "verified")
        assert svc.auth.last.verdict == "rejected"
        [event] = db.security_events()
        assert event["kind"] == "unknown_voice" and event["voice_conf"] < 0.2
    finally:
        svc.stop()
