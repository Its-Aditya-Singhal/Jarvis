"""Speech round trip with the real local models: Kokoro speaks, Whisper listens."""

import numpy as np
import pytest
from conftest import MODELS
from scipy.signal import resample_poly

from jarvis.speech.stt import SpeechToText
from jarvis.speech.text import phrase_match
from jarvis.speech.tts import SAMPLE_RATE, TextToSpeech
from jarvis.speech.wake import find_wake
from jarvis.speech_service import name_prompt

pytestmark = pytest.mark.models


@pytest.fixture(scope="module")
def tts():
    t = TextToSpeech(MODELS)
    if not t.load():
        pytest.skip("voice synthesis model not downloaded")
    return t


@pytest.fixture(scope="module")
def stt():
    s = SpeechToText(MODELS)
    if not s.load():
        pytest.skip("speech recognition model not downloaded")
    return s


def to16k(audio: np.ndarray) -> np.ndarray:
    assert SAMPLE_RATE == 24000
    return resample_poly(audio, 2, 3).astype(np.float32)


@pytest.mark.parametrize("gender", ["female", "male"])
def test_both_voices_speak_english_and_hindi(tts, gender):
    for text in ("Authentication approved.", "नमस्ते, मैं तैयार हूँ।"):
        audio = tts.synth(text, gender)
        assert 0.5 < len(audio) / SAMPLE_RATE < 6 and np.abs(audio).max() > 0.05
    # the two genders are different voices
    other = "male" if gender == "female" else "female"
    assert tts.voice_for("hi", gender) != tts.voice_for("hi", other)


def test_english_command_round_trip(tts, stt):
    # the prompt must not swallow the spoken name (an ALL-CAPS name, as users type it)
    tr = stt.transcribe(to16k(tts.synth("Friday, set an alarm for seven in the morning.", "male")), prompt=name_prompt("FRIDAY"))
    assert tr.language == "en"
    found, command = find_wake("Friday", tr.text)
    assert found and "alarm" in command.lower() and "seven" in command.lower()


def test_hindi_enrollment_phrase_is_recognised(tts, stt):
    audio = to16k(tts.synth("कल सुबह सात बजे मुझे जगाना।", "female"))
    tr = stt.transcribe(audio)
    assert tr.language == "hi"
    assert phrase_match("kal subah saat baje mujhe jagana.", tr.text) > 0.8
    assert phrase_match("Aaj ka weather kaisa hai, zara batao na.", tr.text) < 0.55
