"""The Gemini brain: one request per command, quota fallback between the two models, no key leaks."""

import json

import httpx
import pytest

from jarvis.brain import Brain
from jarvis.config import Settings
from jarvis.llm.client import LLMUnavailable
from jarvis.llm.gemini import FAST_MODEL, HEAVY_MODEL, BadKey, GeminiClient, QuotaExceeded, extract_json


class KV(dict):
    def get(self, k, default=None):
        return super().get(k, default)

    def set(self, k, v):
        self[k] = v


def answer(text: str) -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


class Api:
    """A scripted Gemini endpoint: ``replies[model]`` is a list of responses (or callables)."""

    def __init__(self, replies):
        self.replies = replies
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        model = request.url.path.rsplit("/", 1)[-1].split(":")[0]
        body = json.loads(request.content)
        self.calls.append((model, body, dict(request.headers)))
        r = self.replies[model].pop(0)
        return r(body) if callable(r) else r


def client(api: Api, key="AIzaTESTKEY1234") -> GeminiClient:
    return GeminiClient(lambda: key, transport=httpx.MockTransport(api))


def brain(api: Api, tmp_path) -> Brain:
    s = Settings(data_dir=tmp_path, llm_provider="gemini")
    return Brain(s, KV(), lambda: ("JARVIS", "Aditya"), lambda: "male", cloud=client(api))


def test_extract_json_from_prose_and_fences():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"actions": [], "reply": "hi"} hope that helps') == {"actions": [], "reply": "hi"}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_a_command_is_one_request_to_the_fast_model(tmp_path):
    api = Api({FAST_MODEL: [answer('{"actions": [{"tool": "app.open", "args": {"name": "Notes"}}], "reply": ""}')]})
    b = brain(api, tmp_path)
    assert b.gemini and b.status() == "ready" and b.start()
    r = b.respond("could you get Notes up for me")
    assert [a.tool for a in r.actions] == ["app.open"] and r.ok and not r.fast
    assert len(api.calls) == 1
    model, body, headers = api.calls[0]
    assert model == FAST_MODEL and headers["x-goog-api-key"] == "AIzaTESTKEY1234"
    assert "systemInstruction" in body and body["generationConfig"]["responseMimeType"] == "application/json"
    assert "AIzaTESTKEY1234" not in json.dumps(body)  # the key travels only in the header


def test_everyday_commands_need_no_request(tmp_path):
    api = Api({FAST_MODEL: []})
    r = brain(api, tmp_path).respond("set the volume to 30")
    assert r.fast and [a.tool for a in r.actions] == ["system.volume"] and api.calls == []


def test_quota_falls_back_to_the_other_model_and_says_so(tmp_path):
    api = Api({
        FAST_MODEL: [httpx.Response(429, json={"error": {"message": "Quota exceeded for requests per minute"}})],
        HEAVY_MODEL: [answer('{"actions": [], "reply": "Paris."}')],
    })
    b = brain(api, tmp_path)
    r = b.respond("what is the capital of France")
    assert r.reply == "Paris." and [c[0] for c in api.calls] == [FAST_MODEL, HEAVY_MODEL]
    assert FAST_MODEL in b.note and HEAVY_MODEL in b.note


def test_both_over_quota_gives_a_clear_message(tmp_path):
    over = {"error": {"message": "Quota exceeded", "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProject"}]}]}}
    api = Api({FAST_MODEL: [httpx.Response(429, json=over)], HEAVY_MODEL: [httpx.Response(429, json=over)]})
    r = brain(api, tmp_path).respond("tell me a joke about cats")
    assert not r.ok and "free Gemini limit" in r.reply and "midnight Pacific" in r.reply
    assert "volume" in r.reply  # everyday commands still work


def test_no_key_tells_the_owner_where_to_add_it(tmp_path):
    s = Settings(data_dir=tmp_path, llm_provider="gemini")
    b = Brain(s, KV(), lambda: ("JARVIS", "Aditya"), lambda: "male", cloud=GeminiClient(lambda: None))
    assert "Settings → AI" in b.status() and not b.start()
    r = b.respond("tell me a joke about cats")
    assert not r.ok and "Settings → AI" in r.reply


def test_options_a_model_refuses_are_dropped_once_and_remembered():
    api = Api({FAST_MODEL: [
        httpx.Response(400, json={"error": {"message": "Thinking level is not supported for this model."}}),
        httpx.Response(400, json={"error": {"message": "JSON mode is not enabled for this model"}}),
        answer('Here you go: {"ok": true}'),
        answer('{"ok": 2}'),
    ]})
    c = client(api)
    assert c.chat_json(FAST_MODEL, [{"role": "user", "content": "hi"}]) == {"ok": True}
    assert c.chat_json(FAST_MODEL, [{"role": "user", "content": "hi"}]) == {"ok": 2}
    last = api.calls[-1][1]["generationConfig"]
    assert "thinkingConfig" not in last and "responseMimeType" not in last and len(api.calls) == 4


def test_errors_are_named():
    for status, body, exc in [
        (400, {"error": {"message": "API key not valid. Please pass a valid API key."}}, BadKey),
        (403, {"error": {"message": "Permission denied"}}, BadKey),
        (404, {"error": {"message": "models/x is not found"}}, LLMUnavailable),
        (429, {"error": {"message": "Resource exhausted"}}, QuotaExceeded),
    ]:
        c = client(Api({FAST_MODEL: [httpx.Response(status, json=body)]}))
        with pytest.raises(exc):
            c.generate(FAST_MODEL, [{"role": "user", "content": "hi"}])


def test_writing_goes_to_the_heavy_model(tmp_path):
    api = Api({HEAVY_MODEL: [answer("  Rahul invited you to his birthday on 17 October.  ")]})
    b = brain(api, tmp_path)
    assert b.write("Summarise.", "email text") == "Rahul invited you to his birthday on 17 October."
    assert api.calls[0][0] == HEAVY_MODEL and "responseMimeType" not in api.calls[0][1]["generationConfig"]


def test_model_names_and_provider_are_settings(tmp_path):
    api = Api({"gemini-3.8-flash": [answer('{"actions": [], "reply": "ok"}')]})
    b = brain(api, tmp_path)
    b.db.set("ai.fast_model", "gemini-3.8-flash")
    b.respond("how are you doing today my friend")
    assert api.calls[0][0] == "gemini-3.8-flash"
    b.db.set("ai.provider", "ollama")
    assert not b.gemini and b.model == b.s.llm_model


def test_ai_settings_keep_the_key_sealed(tmp_path):
    from test_api import H, _client, _voice_ready

    s = Settings(data_dir=tmp_path / "data", api_token="test-token", face_auth=False, llm_provider="gemini")
    c, svc = _client(s, llm=True, voice=True)
    with c:
        _voice_ready(svc)
        c.post("/api/setup/profile", headers=H, json={"owner_name": "Aditya", "assistant_name": "JARVIS"})
        svc.voice = None  # no microphone: setup completes typed-only, so level 2 comes from the keyboard rule
        assert c.post("/api/setup/complete", headers=H).status_code == 200
        st = c.get("/api/settings/ai", headers=H).json()
        assert st["provider"] == "gemini" and st["key"] is None and "Settings → AI" in st["status"]
        assert svc.guard.services() == ()
        r = c.put("/api/settings/ai", headers=H, json={"api_key": "AIzaSyTESTKEY-abcd1234"})
        assert r.status_code == 200 and r.json()["key"] == "…1234" and r.json()["status"] == "ready"
        assert "AIzaSyTESTKEY" not in json.dumps(c.get("/api/settings/ai", headers=H).json())
        raw = svc.db.get("secret.gemini_api_key")
        assert raw and "AIzaSy" not in raw  # sealed at rest
        assert svc.guard.services() == ("generativelanguage.googleapis.com", "open-meteo.com")  # + weather (a city name)
        r = c.put("/api/settings/ai", headers=H, json={"heavy_model": "gemini-3.8-flash"})
        assert r.json()["heavy_model"] == "gemini-3.8-flash"
        assert c.put("/api/settings/ai", headers=H, json={"fast_model": "../../etc"}).status_code == 422
        assert c.put("/api/settings/ai", headers=H, json={"api_key": "not a key!"}).status_code == 400
        # what a copy from a web page brings along is dropped; the provider's Test decides the rest
        pasted = ' \u200b"AIzaSyPASTED-key_9876"\n '
        r = c.put("/api/settings/ai", headers=H, json={"api_key": pasted})
        assert r.status_code == 200 and r.json()["key"] == "…9876"
        assert c.put("/api/settings/ai", headers=H, json={"api_key": "AQ.Ab8RN6Lnew.format-key"}).status_code == 200
        assert c.put("/api/settings/ai", headers=H, json={"api_key": "short"}).status_code == 400
        assert c.put("/api/settings/ai", headers=H, json={"api_key": "AIzaSy\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9"}).status_code == 400
        c.put("/api/settings/ai", headers=H, json={"api_key": ""})
        assert svc.guard.services() == () and c.get("/api/settings/ai", headers=H).json()["key"] is None
