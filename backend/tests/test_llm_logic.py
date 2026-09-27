"""LLM layer against a mock Ollama server (no model needed)."""

import json
from datetime import datetime

import httpx

from jarvis.brain import Brain
from jarvis.llm.client import OllamaClient
from jarvis.llm.intents import TOOLS, build_messages, compose_reply, detect_language, parse_intent

NOW = datetime(2026, 9, 27, 21, 30)


class KV:
    def __init__(self): self.kv = {}
    def get(self, k, d=None): return self.kv.get(k, d)
    def set(self, k, v): self.kv[k] = v


class NoServer:
    error = None
    def ensure(self): return True
    def stop(self): ...


def mock_ollama(answers, models=("qwen2.5:7b",), seen=None):
    answers = list(answers)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in models]})
        if req.url.path == "/api/generate":
            return httpx.Response(200, json={"done": True})
        if req.url.path == "/api/chat":
            body = json.loads(req.content)
            if seen is not None:
                seen.append(body)
            if body["model"] not in models:
                return httpx.Response(404, json={"error": "model not found"})
            a = answers.pop(0)
            return httpx.Response(200, json={"message": {"content": a if isinstance(a, str) else json.dumps(a)}})
        return httpx.Response(404)

    return OllamaClient("http://ollama.test", transport=httpx.MockTransport(handler))


def make_brain(settings, client, gender="female"):
    return Brain(settings, KV(), lambda: ("FRIDAY", "Aditya"), lambda: gender, server=NoServer(), client=client,
                 clock=lambda: datetime(2026, 9, 27, 21, 30))


def test_prompt_is_cacheable_and_time_travels_with_the_message():
    a = build_messages("FRIDAY", "Aditya", NOW, "en", "hi", [])
    b = build_messages("FRIDAY", "Aditya", datetime(2026, 9, 28, 8, 0), "hi", "नमस्ते", [])
    assert a[:-1] == b[:-1]  # identical prefix -> Ollama reuses its cache
    sys = a[0]["content"]
    assert "FRIDAY" in sys and "Aditya" in sys and all(t in sys for t in TOOLS) and "Never say" in sys
    assert a[-1]["content"] == "[Now: Sunday 2026-09-27 21:30 | Language: English]\nhi"
    assert "Language: Hindi" in b[-1]["content"]


def test_language_is_decided_in_code():
    assert detect_language("What's the time?") == "en"
    assert detect_language("कितने बजे हैं") == "hi"
    assert detect_language("kal subah saat baje mujhe jagana") == "hinglish"
    assert detect_language("Spotify khol do") == "hinglish"
    assert detect_language("Open Spotify") == "en"
    assert detect_language("okay", stt_lang="hi") == "hinglish"


def test_parse_drops_unknown_tools_and_bad_fields():
    i = parse_intent({"actions": [
        {"tool": "shell.run", "args": {"cmd": "rm -rf ~"}},
        {"tool": "notes.add", "args": "oops"},
    ], "reply": "  ok  "}, "en", NOW)
    assert [a.tool for a in i.actions] == ["notes.add"]
    assert i.actions[0].args == {} and i.actions[0].summary == "save a note" and i.reply == "ok"


def test_action_replies_never_claim_success():
    # the model's own wording ("Done! Alarm set.") never reaches the user for actions
    i = parse_intent({"reply": "Done! Alarm set.", "actions": [
        {"tool": "alarm.set", "args": {"time": "2026-09-28T07:00:00-07:00"}, "summary": "Alarm set!"}]}, "en", NOW)
    r = compose_reply(i, "female")
    assert r == "Understood: an alarm for 7:00 AM tomorrow. I can't carry out actions yet; that arrives with the tools update."
    hi = parse_intent({"reply": "", "actions": [
        {"tool": "alarm.set", "args": {"time": "2026-09-28T19:15"}},
        {"tool": "timer.set", "args": {"seconds": 600}}]}, "hinglish", NOW)
    assert compose_reply(hi, "male").startswith("समझ गया: कल शाम 7:15 बजे का अलार्म और 10 मिनट का टाइमर।")
    assert "सकती" in compose_reply(hi, "female")


def test_brain_chat_compound_and_history(settings):
    seen = []
    client = mock_ollama([
        {"actions": [], "reply": "Photosynthesis turns light into chemical energy."},
        {"reply": "", "actions": [
            {"tool": "alarm.set", "args": {"time": "2026-09-28T07:00"}},
            {"tool": "notes.add", "args": {"text": "buy milk"}}]},
    ], seen=seen)
    b = make_brain(settings, client)
    assert b.status() == "ready"
    r = b.respond("What is photosynthesis?")
    assert r.ok and r.reply.startswith("Photosynthesis") and r.actions == []
    r = b.respond("Wake me at 7 tomorrow and note to buy milk")
    assert [a.tool for a in r.actions] == ["alarm.set", "notes.add"]
    assert "an alarm for 7:00 AM tomorrow and a note: “buy milk”" in r.reply
    # the first exchange was sent back as context (after the few-shot examples), with the schema
    msgs = seen[1]["messages"]
    assert msgs[-3]["content"].endswith("What is photosynthesis?") and seen[1]["format"]["required"]
    b.clear()


def test_model_missing_or_server_down_is_reported(settings):
    b = make_brain(settings, mock_ollama([], models=("llama3.2:3b",)))
    assert "not installed" in b.status() and "ollama pull qwen2.5:7b" in b.status()
    r = b.respond("hello")
    assert not r.ok and "isn't available" in r.reply
    down = OllamaClient("http://127.0.0.1:9", timeout_s=0.5)
    b2 = make_brain(settings, down)
    r = b2.respond("कैसे हो", stt_lang="hi")
    assert not r.ok and "उपलब्ध नहीं" in r.reply


def test_invalid_json_is_retried_then_apologised(settings):
    b = make_brain(settings, mock_ollama(["not json", {"actions": [], "reply": "Hi Aditya."}]))
    assert b.respond("hey").reply == "Hi Aditya."
    b = make_brain(settings, mock_ollama(["bad", "worse"]), gender="male")
    assert b.respond("नमस्ते", stt_lang="hi").reply.endswith("पाया।")


def test_switch_model_only_to_installed(settings):
    b = make_brain(settings, mock_ollama([], models=("qwen2.5:7b", "llama3.2:3b")))
    b.set_model("llama3.2:3b")
    assert b.model == "llama3.2:3b"
    import pytest
    with pytest.raises(ValueError):
        b.set_model("gpt-4")
