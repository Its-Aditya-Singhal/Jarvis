"""Intent extraction with the real local model (skipped if Ollama/model absent)."""

from datetime import datetime

import pytest

from jarvis.brain import Brain
from jarvis.llm.client import LLMUnavailable, OllamaClient
from jarvis.llm.server import OllamaServer
from jarvis.speech.text import has_devanagari

pytestmark = pytest.mark.ollama


class KV:
    def __init__(self): self.kv = {}
    def get(self, k, d=None): return self.kv.get(k, d)
    def set(self, k, v): self.kv[k] = v


@pytest.fixture(scope="module")
def brain(tmp_path_factory):
    from jarvis.config import Settings

    s = Settings(data_dir=tmp_path_factory.mktemp("llm"))
    client = OllamaClient(f"http://{s.ollama_host}")
    try:
        if s.llm_model not in client.models():
            pytest.skip(f"{s.llm_model} not pulled")
    except LLMUnavailable:
        pytest.skip("Ollama not running")
    b = Brain(s, KV(), lambda: ("FRIDAY", "Aditya"), lambda: "female",
              server=OllamaServer(s.ollama_host, None, s.data_dir), client=client,
              clock=lambda: datetime(2026, 9, 27, 21, 30))
    b.client.warm(b.model)
    yield b
    b.client.unload(b.model)  # don't leave gigabytes resident after the tests


def tools(r):
    return [a.tool for a in r.actions]


def test_english_alarm_resolves_tomorrow(brain):
    brain.clear()
    r = brain.respond("Set an alarm for 7 am tomorrow")
    assert r.ok and tools(r) == ["alarm.set"] and r.language == "en"
    assert str(r.actions[0].args.get("time", "")).startswith("2026-09-28T07:00")
    assert "can't carry out" in r.reply  # never claims it was done


def test_hinglish_compound_request(brain):
    brain.clear()
    r = brain.respond("kal subah saat baje mujhe jagana aur doodh lene ka note bana do")
    assert r.ok and tools(r) == ["alarm.set", "notes.add"]
    assert r.language in ("hinglish", "hi") and has_devanagari(r.reply)


def test_question_gets_an_answer_without_actions(brain):
    brain.clear()
    r = brain.respond("What is the capital of France?")
    assert r.ok and tools(r) == [] and "Paris" in r.reply


def test_unsupported_request_is_refused_not_invented(brain):
    brain.clear()
    r = brain.respond("Send an email to my boss saying I'm late")
    assert r.ok and tools(r) == []


def test_hindi_question_answered_in_hindi(brain):
    brain.clear()
    r = brain.respond("भारत की राजधानी क्या है?", stt_lang="hi")
    assert r.ok and tools(r) == [] and has_devanagari(r.reply) and "दिल्ली" in r.reply
