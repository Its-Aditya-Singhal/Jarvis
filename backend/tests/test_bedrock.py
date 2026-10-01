"""Claude on Amazon Bedrock: opt-in, any model per tier, Gemini answers when Bedrock can't, key sealed."""

import json

import httpx
import httpx2
import pytest

from jarvis.brain import Brain
from jarvis.config import Settings
from jarvis.llm import bedrock as br
from jarvis.llm.bedrock import BedrockClient
from jarvis.llm.client import LLMUnavailable
from jarvis.llm.gemini import FAST_MODEL as G_FAST
from jarvis.llm.gemini import BadKey, GeminiClient, QuotaExceeded

KEY = "ABSKQmVkcm9ja0FQSUtleS10ZXN0LWF0LTEyMzQ1Njc4OTA="


class KV(dict):
    def get(self, k, default=None):
        return super().get(k, default)

    def set(self, k, v):
        self[k] = v


def msg(text: str, stop: str = "end_turn") -> httpx2.Response:
    return httpx2.Response(200, json={
        "id": "msg_1", "type": "message", "role": "assistant", "model": "x", "stop_reason": stop,
        "stop_sequence": None, "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })


def err(status: int, message: str) -> httpx2.Response:
    return httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": message}})


class Aws:
    """A scripted Bedrock endpoint: ``replies[model]`` is a list of responses."""

    def __init__(self, replies):
        self.replies = replies
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.calls.append((str(request.url), body, dict(request.headers)))
        return self.replies[body["model"]].pop(0)


def client(aws: Aws, key: str | None = KEY, region: str = "us-east-1") -> BedrockClient:
    return BedrockClient(lambda: key, lambda: region, transport=httpx2.MockTransport(aws))


def gemini(replies: dict, key: str | None = "AIzaTESTKEY1234") -> tuple[GeminiClient, list]:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        model = request.url.path.rsplit("/", 1)[-1].split(":")[0]
        calls.append(model)
        text = replies[model].pop(0)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})

    return GeminiClient(lambda: key, transport=httpx.MockTransport(handle)), calls


def brain(aws: Aws, tmp_path, cloud: GeminiClient | None = None, key: str | None = KEY) -> Brain:
    db = KV({"ai.provider": "bedrock"})
    s = Settings(data_dir=tmp_path, llm_provider="gemini")
    return Brain(s, db, lambda: ("JARVIS", "Aditya"), lambda: "male",
                 cloud=cloud or GeminiClient(lambda: None), bedrock=client(aws, key))


def test_a_command_goes_to_the_haiku_tier_on_the_bedrock_messages_endpoint(tmp_path):
    aws = Aws({br.FAST_MODEL: [msg('{"actions": [{"tool": "app.open", "args": {"name": "Notes"}}], "reply": ""}')]})
    b = brain(aws, tmp_path)
    assert b.provider == "bedrock" and b.remote and b.status() == "ready" and b.start()
    r = b.respond("could you get Notes up for me")
    assert [a.tool for a in r.actions] == ["app.open"] and r.ok
    url, body, headers = aws.calls[0]
    assert url == "https://bedrock-mantle.us-east-1.api.aws/anthropic/v1/messages"
    assert headers["x-api-key"] == KEY and KEY not in json.dumps(body)  # the key travels only in the header
    assert body["model"] == "anthropic.claude-haiku-4-5" and body["thinking"] == {"type": "disabled"}
    assert "JSON object" in body["system"]  # no structured outputs on Bedrock: asked for in the prompt


def test_any_claude_model_and_region_are_settings(tmp_path):
    draft = '{"subject": "Dinner", "body": "See you Friday."}'
    aws = Aws({"anthropic.claude-opus-5-5": [msg(draft), msg("Long thread summary.")],
               "anthropic.claude-sonnet-5-5": [msg("Rahul invited you to his birthday."),
                                               msg('{"actions": [], "reply": "hi"}')]})
    b = brain(aws, tmp_path)
    b.db.set("ai.bedrock_heavy_model", "anthropic.claude-opus-5-5")
    b.db.set("ai.bedrock_fast_model", "anthropic.claude-sonnet-5-5")
    assert b.write_json("Draft a mail.", "dinner on Friday")["subject"] == "Dinner"  # drafting: the strong model
    assert b.write("Summarise.", "email text") == "Rahul invited you to his birthday."  # summary: the fast one
    assert b.write("Summarise.", "x" * 20000) == "Long thread summary."  # a long thread: the strong one
    b.respond("how are you doing today my friend")
    assert [c[1]["model"] for c in aws.calls] == ["anthropic.claude-opus-5-5", "anthropic.claude-sonnet-5-5",
                                                   "anthropic.claude-opus-5-5", "anthropic.claude-sonnet-5-5"]
    b.bedrock.region = lambda: "eu-west-1"
    aws.replies["anthropic.claude-sonnet-5-5"] = [msg('{"actions": [], "reply": "hi"}')]
    b.respond("how are you doing today my friend")
    assert aws.calls[-1][0].startswith("https://bedrock-mantle.eu-west-1.api.aws/")


def test_options_a_model_refuses_are_dropped_once_and_remembered():
    m = "anthropic.claude-opus-5-5"
    aws = Aws({m: [
        err(400, '"thinking.type.disabled" is not supported for this model.'),
        err(400, '"thinking.type.between_tools" is not supported for this model.'),
        err(400, "temperature is not supported for this model"),
        msg('Sure: {"ok": true}'),
        msg('{"ok": 2}'),
    ]})
    c = client(aws)
    assert c.chat_json(m, [{"role": "user", "content": "hi"}]) == {"ok": True}
    assert c.chat_json(m, [{"role": "user", "content": "hi"}]) == {"ok": 2}
    last = aws.calls[-1][1]
    assert "thinking" not in last and "temperature" not in last and last["output_config"] == {"effort": "low"}
    assert last["max_tokens"] >= 4096 and len(aws.calls) == 5


def test_errors_are_named_in_plain_words():
    m = br.FAST_MODEL
    for status, text, exc, words in [
        (401, "The security token included in the request is invalid", BadKey, "expired"),
        (403, "You don't have access to the model with the specified model ID.", BadKey, "Model access"),
        (404, "model not found", LLMUnavailable, "isn't available in us-east-1"),
        (429, "Too many requests, please wait", QuotaExceeded, "throttled"),
        (400, "Invocation with on-demand throughput isn't supported", LLMUnavailable, "different model id"),
    ]:
        c = client(Aws({m: [err(status, text)] * 3}))
        with pytest.raises(exc) as e:
            c.generate(m, [{"role": "user", "content": "hi"}])
        assert words in str(e.value), (status, str(e.value))
        assert KEY not in str(e.value)
    with pytest.raises(BadKey, match="Settings → AI"):
        client(Aws({}), key=None).generate(m, [{"role": "user", "content": "hi"}])


def test_a_refusal_is_not_an_answer():
    c = client(Aws({br.FAST_MODEL: [msg("", stop="refusal")]}))
    with pytest.raises(ValueError):
        c.generate(br.FAST_MODEL, [{"role": "user", "content": "hi"}])


def test_gemini_answers_when_bedrock_cant_and_says_why(tmp_path):
    aws = Aws({br.FAST_MODEL: [err(403, "no access")], br.HEAVY_MODEL: [err(403, "no access")]})
    cloud, gcalls = gemini({G_FAST: ['{"actions": [], "reply": "Paris."}']})
    b = brain(aws, tmp_path, cloud=cloud)
    r = b.respond("what is the capital of France")
    assert r.ok and r.reply == "Paris." and gcalls == [G_FAST]
    assert "Bedrock couldn't answer" in b.note and "Model access" in b.note


def test_throttled_haiku_tries_the_other_bedrock_model_first(tmp_path):
    aws = Aws({br.FAST_MODEL: [err(429, "slow down")], br.HEAVY_MODEL: [msg('{"actions": [], "reply": "Paris."}')]})
    b = brain(aws, tmp_path)
    r = b.respond("what is the capital of France")
    assert r.reply == "Paris." and [c[1]["model"] for c in aws.calls] == [br.FAST_MODEL, br.HEAVY_MODEL]


def test_without_a_gemini_key_bedrock_errors_reach_the_owner(tmp_path):
    over = [err(429, "slow down")]
    r = brain(Aws({br.FAST_MODEL: list(over), br.HEAVY_MODEL: list(over)}), tmp_path).respond("tell me a joke about cats")
    assert not r.ok and "Bedrock is throttling" in r.reply and "Gemini" not in r.reply
    b = brain(Aws({}), tmp_path, key=None)
    assert "Bedrock API key" in b.status()
    r = b.respond("tell me a joke about cats")
    assert not r.ok and "Settings → AI" in r.reply


def test_bedrock_without_a_client_falls_back_to_gemini(tmp_path):
    s = Settings(data_dir=tmp_path, llm_provider="gemini")
    b = Brain(s, KV({"ai.provider": "bedrock"}), lambda: ("JARVIS", "Aditya"), lambda: "male",
              cloud=GeminiClient(lambda: None))
    assert b.provider == "gemini"


def test_bedrock_settings_keep_the_key_sealed_and_open_only_its_host(tmp_path):
    from test_api import H, _client, _voice_ready

    s = Settings(data_dir=tmp_path / "data", api_token="test-token", face_auth=False, llm_provider="gemini")
    c, svc = _client(s, llm=True, voice=True)
    with c:
        _voice_ready(svc)
        c.post("/api/setup/profile", headers=H, json={"owner_name": "Aditya", "assistant_name": "JARVIS"})
        voice, svc.voice = svc.voice, None  # stopped at the end: its thread mustn't outlive this test
        assert c.post("/api/setup/complete", headers=H).status_code == 200
        st = c.get("/api/settings/ai", headers=H).json()
        assert st["bedrock"]["key"] is None and st["bedrock"]["fast_model"] == br.FAST_MODEL
        assert "anthropic.claude-opus-5-5" in st["bedrock"]["models"]
        r = c.put("/api/settings/ai", headers=H, json={"provider": "bedrock"})
        assert r.json()["provider"] == "bedrock" and "Bedrock API key" in r.json()["status"]
        assert svc.guard.services() == ()  # nothing goes out without a key
        r = c.put("/api/settings/ai", headers=H, json={"bedrock_key": KEY})
        assert r.status_code == 200 and r.json()["bedrock"]["key"] == "…" + KEY[-4:] and r.json()["status"] == "ready"
        assert KEY not in json.dumps(c.get("/api/settings/ai", headers=H).json())
        raw = svc.db.get("secret.bedrock_api_key")
        assert raw and "ABSK" not in raw  # sealed at rest
        assert svc.guard.services() == ("bedrock-mantle.us-east-1.api.aws", "open-meteo.com")  # + weather (a city name)
        c.put("/api/settings/ai", headers=H, json={"api_key": "AIzaSyTESTKEY-abcd1234"})  # Gemini as the fallback
        assert svc.guard.services() == ("bedrock-mantle.us-east-1.api.aws", "generativelanguage.googleapis.com", "open-meteo.com")
        r = c.put("/api/settings/ai", headers=H, json={"bedrock_region": "eu-west-1",
                                                       "bedrock_heavy_model": "anthropic.claude-opus-5-5"})
        assert r.json()["bedrock"]["heavy_model"] == "anthropic.claude-opus-5-5"
        assert svc.brain.heavy_model == "anthropic.claude-opus-5-5"
        assert svc.guard.services()[0] == "bedrock-mantle.eu-west-1.api.aws"
        for bad in ({"bedrock_region": "evil.com/"}, {"bedrock_fast_model": "../../etc"}):
            assert c.put("/api/settings/ai", headers=H, json=bad).status_code == 422
        assert c.put("/api/settings/ai", headers=H, json={"bedrock_key": "not a key!"}).status_code == 400
        c.put("/api/settings/ai", headers=H, json={"bedrock_key": ""})
        assert c.get("/api/settings/ai", headers=H).json()["bedrock"]["key"] is None
        assert svc.guard.services() == ("generativelanguage.googleapis.com", "open-meteo.com")  # Gemini still answers
        c.put("/api/settings/ai", headers=H, json={"provider": "gemini"})
        assert svc.guard.services() == ("generativelanguage.googleapis.com", "open-meteo.com")
    voice.stop()


def test_the_model_is_picked_by_the_command(tmp_path):
    ok = '{"actions": [], "reply": "ok"}'
    aws = Aws({br.FAST_MODEL: [msg(ok), msg(ok)], br.HEAVY_MODEL: [msg(ok), msg(ok), msg(ok)]})
    b = brain(aws, tmp_path)
    for text in ["could you get Notes up for me", "summarize the mail from Rahul"]:  # simple, reading mail: Haiku
        b.respond(text)
    for text in ["draft a reply to Priya about dinner", "plan my trip to Goa next weekend",
                 "find the cheapest way to get to the airport and book a cab for nine in the morning tomorrow"]:
        b.respond(text)  # writing, planning, long: the stronger model
    assert [c[1]["model"] for c in aws.calls] == [br.FAST_MODEL] * 2 + [br.HEAVY_MODEL] * 3


def test_gemini_keeps_the_fast_model_first_for_every_command(tmp_path):
    from jarvis.brain import sounds_hard

    assert sounds_hard("draft a reply to Priya") and not sounds_hard("open Safari")
    assert not sounds_hard("summarize my last 10 emails")
    cloud, calls = gemini({G_FAST: ['{"actions": [], "reply": "ok"}']})
    s = Settings(data_dir=tmp_path, llm_provider="gemini")
    Brain(s, KV(), lambda: ("JARVIS", "Aditya"), lambda: "male", cloud=cloud).respond("summarize the mail from Rahul")
    assert calls == [G_FAST]

