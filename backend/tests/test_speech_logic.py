"""Wake word, transliteration and phrase matching (no models needed)."""

import numpy as np

from jarvis.events import EventBus
from jarvis.speech.output import SpeechOutput
from jarvis.speech.stt import Transcript
from jarvis.speech.text import phrase_match, skeleton, split_sentences, to_latin
from jarvis.speech.tts import TextToSpeech
from jarvis.speech.wake import find_wake
from jarvis.speech_service import SpeechService, placeholder_reply


def test_devanagari_and_romanised_hindi_compare_equal():
    assert to_latin("कल सुबह सात बजे") == "kal subah sat baje"
    assert skeleton("कल सुबह सात बजे मुझे जगाना") == skeleton("kal subah saat baje mujhe jagana")


def test_phrase_match_accepts_either_script_and_rejects_other_phrases():
    expected = "Friday, mera calendar check kar."
    assert phrase_match(expected, "फ्राइडे, मेरा कैलेंडर चेक कर") > 0.8
    assert phrase_match(expected, "Friday, mera calendar check kar") > 0.95
    assert phrase_match(expected, "Aaj ka weather kaisa hai") < 0.55
    assert phrase_match(expected, "") == 0.0


def test_wake_word_uses_the_chosen_name():
    assert find_wake("Friday", "Hey Friday, what's the time?") == (True, "what's the time")
    assert find_wake("Friday", "Friday.") == (True, "")
    assert find_wake("Friday", "Fryday open notes") == (True, "open notes")  # STT spelling slip
    assert find_wake("Friday", "फ्राइडे, लाइट बंद करो") == (True, "लाइट बंद करो")
    assert find_wake("Jarvis", "जार्विस अलार्म लगाओ")[0]
    assert find_wake("Friday", "Jarvis set an alarm") == (False, "")  # a different name
    assert find_wake("Friday", "I told Friday about it") == (False, "")  # mentioned, not addressed
    assert find_wake("Tony Stark", "Hey Tony Stark, open notes") == (True, "open notes")


def test_sentences_and_voices():
    assert split_sentences("Hi. How are you? मैं ठीक हूँ। Bye!") == ["Hi.", "How are you?", "मैं ठीक हूँ।", "Bye!"]
    assert TextToSpeech.voice_for("Hello", "female") == ("af_heart", "en-us")
    assert TextToSpeech.voice_for("Hello", "male") == ("am_michael", "en-us")
    assert TextToSpeech.voice_for("नमस्ते", "male") == ("hm_omega", "hi")
    assert "पाऊँगा" in placeholder_reply("लाइट बंद करो", "hi", "male")
    assert "पाऊँगी" in placeholder_reply("लाइट बंद करो", "hi", "female")


# -- conversation turns with fake STT/TTS --------------------------------------------
class FakeSTT:
    ready, size, error = True, "fake", None

    def __init__(self): self.text, self.lang = "", "en"
    def load(self): return True
    def transcribe(self, audio, prompt=None, language=None):
        return Transcript(self.text, self.lang, 1.0, len(audio) / 16000)


class FakeTTS:
    ready, error = True, None

    def load(self): return True
    def synth(self, text, gender): return np.zeros(240, np.float32)


class FakeDB:
    def __init__(self): self.kv, self.events = {}, []
    def get(self, k, d=None): return self.kv.get(k, d)
    def set(self, k, v): self.kv[k] = v
    def add_security_event(self, kind, detail, **kw): self.events.append(kind)


def make(settings, verified=True):
    bus = EventBus()
    seen = []
    for kind in ("heard", "reply", "listening"):
        bus.on(kind, seen.append)
    state = {"verified": verified}
    sp = SpeechService(settings, FakeDB(), bus, FakeSTT(), FakeTTS(), lambda: state["verified"], lambda: ("Friday", "Aditya"))
    said = []
    sp.out.say = lambda text, gender=None: said.append(text)
    sp.out.ping = lambda: said.append("<ping>")
    return sp, seen, said, state


AUDIO = np.zeros(16000, np.float32)


def test_unaddressed_speech_is_discarded(settings):
    sp, seen, said, _ = make(settings)
    sp.stt.text = "so I was telling him about the weekend"
    sp.handle(AUDIO, "verified")
    assert seen == [] and said == []


def test_command_needs_verified_owner_and_matching_voice(settings):
    sp, seen, said, state = make(settings, verified=False)
    sp.stt.text = "Friday, open my notes"
    sp.handle(AUDIO, "verified")
    assert seen == [] and "Authentication required" in said[-1]
    assert sp.db.events == ["unauthorized_command"]
    state["verified"] = True
    sp.handle(AUDIO, "rejected")
    assert seen == [] and sp.db.events[-1] == "voice_mismatch_command"
    sp.handle(AUDIO, "verified")
    assert seen[0] == {"type": "heard", "text": "Open my notes", "lang": "en", "stt_s": seen[0]["stt_s"]}
    assert seen[1]["type"] == "reply" and "language model" in said[-1]


def test_long_background_speech_is_checked_on_its_start_and_never_voice_matched(settings):
    sp, seen, said, _ = make(settings)
    lengths, judged = [], []
    transcribe = sp.stt.transcribe
    sp.stt.transcribe = lambda audio, **kw: (lengths.append(len(audio)), transcribe(audio, **kw))[1]
    sp.stt.text = "so anyway I was telling him about the weekend and then we went"
    sp.handle(np.zeros(16000 * 10, np.float32), lambda: judged.append(1) or "verified")
    assert lengths == [int(16000 * 2.5)] and judged == [] and seen == [] and said == []
    # addressed: the whole utterance is transcribed and only then is the voice judged
    sp.stt.text = "Friday, set a timer for five minutes and then remind me about the laundry"
    sp.handle(np.zeros(16000 * 10, np.float32), lambda: judged.append(1) or "verified")
    assert lengths[1:] == [int(16000 * 2.5), 16000 * 10] and judged == [1]


def test_a_failed_voice_match_does_not_block_everyday_commands(settings):
    sp, seen, said, _ = make(settings)
    commands = []
    sp.on_command = lambda text, lang: commands.append(text)
    sp.everyday = lambda text: "brightness" in text
    sp.stt.text = "Friday, decrease brightness"
    sp.handle(AUDIO, "rejected")
    assert commands == ["Decrease brightness"] and sp.db.events == []
    sp.stt.text = "Friday, read my notes"
    sp.handle(AUDIO, lambda: "rejected")
    assert commands == ["Decrease brightness"] and sp.db.events == ["voice_mismatch_command"]


def test_name_alone_opens_a_follow_up_window(settings):
    sp, seen, said, _ = make(settings)
    sp.stt.text = "Friday?"
    sp.handle(AUDIO, None)
    assert said[-1] == "<ping>" and sp.listening  # a blip, not a synthesised "Yes?"
    sp.stt.text = "what's on my calendar"  # no name needed now
    sp.handle(AUDIO, "verified")
    heard = [e for e in seen if e["type"] == "heard"]
    assert heard[-1]["text"] == "What's on my calendar" and not sp.listening
    assert {"type": "listening", "active": False} in seen


class RecordingPlayer:
    def __init__(self): self.played = []
    def play(self, audio, sr, on_level, stop):
        on_level(0.5)
        self.played.append(len(audio))


def test_speech_output_mutes_while_speaking():
    import time

    bus = EventBus()
    events = []
    bus.on("tts", events.append)
    player = RecordingPlayer()
    out = SpeechOutput(FakeTTS(), bus, lambda: "female", player=player)
    out.start()
    out.say("First sentence. Second sentence.")
    assert out.muted()
    for _ in range(100):
        if len(events) == 2:
            break
        time.sleep(0.01)
    assert len(player.played) == 2  # synthesised sentence by sentence
    assert [e["active"] for e in events] == [True, False]
    assert out.muted()  # echo tail
    time.sleep(0.45)
    assert not out.muted()
    out.close()


def test_replies_before_the_voice_model_loads_are_spoken_once_it_has():
    """Face verification starts before speech synthesis has loaded: the greeting must wait, not vanish."""
    import time

    from jarvis.events import EventBus
    from jarvis.fakes import FakePlayer, FakeTTS, Scene
    from jarvis.speech.output import SpeechOutput

    tts = FakeTTS(Scene())
    bus = EventBus()
    spoken = []
    bus.on("tts", lambda e: spoken.append(e["text"]) if e.get("active") else None)
    out = SpeechOutput(tts, bus, lambda: "female", player=FakePlayer())
    out.say("Authentication approved. Hi Aditya.")  # still loading
    tts.load()
    out.start()
    end = time.monotonic() + 3
    while not spoken and time.monotonic() < end:
        time.sleep(0.02)
    out.close()
    assert spoken == ["Authentication approved. Hi Aditya."]

    failed = FakeTTS(Scene())
    failed.error = "voice synthesis model missing"
    out = SpeechOutput(failed, bus, lambda: "female", player=FakePlayer())
    out.say("Hello")  # a voice that can't load: nothing is kept
    assert out._q.empty()
    out.close()
