"""Google's Gemini API (free tier) as JARVIS's brain: nothing runs on the Mac.

Two models, both settings: a fast one that turns a command into tool calls (Gemma 4 26B-A4B by
default: 30 requests a minute on the free tier, and only ~4B parameters active per token, so
it answers quickly) and a stronger one that reads and writes (email summaries, drafts, document
summaries: Gemini 3.5 Flash-Lite, 15 requests a minute). When one model's free quota runs out
the caller tries the other and says so.

One request per command: the reply is JSON in the same schema the local model used, so every
tool keeps its argument checks. Model differences are learned from the API's own errors (a model
that refuses JSON mode, thinking settings or system instructions is retried without them once and
remembered), never assumed from the name.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
from collections.abc import Callable
from typing import Any

import httpx

from .client import LLMUnavailable

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta"
HOST = "generativelanguage.googleapis.com"
FAST_MODEL = "gemma-4-26b-a4b-it"
HEAVY_MODEL = "gemini-3.5-flash-lite"
KEY_PAGE = "https://aistudio.google.com/apikey"
GEMINI_KEY = "gemini_api_key"  # its name in the sealed secrets store
MODEL_NAME = r"^[a-z0-9][a-z0-9.\-]{1,63}$"


class QuotaExceeded(LLMUnavailable):
    """The free tier's per-minute or per-day limit for this model is used up (``provider`` "bedrock":
    AWS is throttling this model)."""

    def __init__(self, model: str, daily: bool, detail: str = "", provider: str = "gemini"):
        self.model, self.daily, self.provider = model, daily, provider
        what = "throttled by Bedrock" if provider == "bedrock" else \
            f"{'daily' if daily else 'per-minute'} free limit reached"
        super().__init__(f"{model}: {what} {detail}".strip())


class BadKey(LLMUnavailable):
    pass


def extract_json(text: str) -> dict[str, Any]:
    """The first JSON object in ``text`` (models without JSON mode wrap it in prose or ``` fences)."""
    text = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", text.strip())
    start = text.find("{")
    while start != -1:
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(data, dict):
            return data
        start = text.find("{", start + 1)
    raise ValueError(f"model returned no JSON object: {text[:200]}")


def _is_daily(body: dict) -> bool:
    """A 429's details name the quota that ran out: per day, or per minute."""
    blob = json.dumps(body).lower()
    return "perday" in blob or "per_day" in blob or "per day" in blob or "daily" in blob


class GeminiClient:
    def __init__(self, api_key: Callable[[], str | None], timeout_s: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        self.api_key = api_key  # read on every call: the owner can change it in Settings
        self._http = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=5.0), transport=transport)
        self._lock = threading.Lock()
        self._no_json: set[str] = set()
        self._no_thinking: set[str] = set()
        self._no_system: set[str] = set()

    # -- the OllamaClient surface the brain uses ------------------------------------------
    def models(self) -> list[str]:
        return []

    def loaded(self) -> dict[str, int]:
        return {}

    def unload(self, model: str) -> None:
        pass

    def warm(self, model: str) -> None:
        pass

    def embed(self, model: str, texts: list[str], keep_alive: str = "") -> list[list[float]]:
        raise LLMUnavailable("memory recall by meaning needs a local embedding model")

    # -- requests -----------------------------------------------------------------------------
    def _body(self, model: str, messages: list[dict[str, str]], json_out: bool, temperature: float,
              max_tokens: int | None) -> dict[str, Any]:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                    for m in messages if m["role"] != "system"]
        body: dict[str, Any] = {"contents": contents}
        if system and model in self._no_system:
            contents.insert(0, {"role": "user", "parts": [{"text": system}]})
            contents.insert(1, {"role": "model", "parts": [{"text": "Understood."}]})
        elif system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        cfg: dict[str, Any] = {"temperature": temperature}
        if max_tokens:
            cfg["maxOutputTokens"] = max_tokens
        if json_out and model not in self._no_json:
            cfg["responseMimeType"] = "application/json"
        if model not in self._no_thinking:
            cfg["thinkingConfig"] = {"thinkingLevel": "minimal"}  # commands need speed, not deliberation
        body["generationConfig"] = cfg
        return body

    def _post(self, model: str, body: dict[str, Any]) -> httpx.Response:
        key = self.api_key()
        if not key:
            raise BadKey("no Gemini API key — add one in Settings → AI")
        try:
            return self._http.post(f"{API}/models/{model}:generateContent", json=body,
                                   headers={"x-goog-api-key": key})
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"Gemini API not reachable: {exc.__class__.__name__}") from exc

    def generate(self, model: str, messages: list[dict[str, str]], json_out: bool = False,
                 temperature: float = 0.2, max_tokens: int | None = None) -> str:
        for _ in range(4):  # at most one retry per unsupported option
            r = self._post(model, self._body(model, messages, json_out, temperature, max_tokens))
            if r.status_code == 200:
                return self._text(r.json())
            body = self._error(r)
            msg = str((body.get("error") or {}).get("message") or r.text[:300])
            low = msg.lower()
            if r.status_code == 429:
                raise QuotaExceeded(model, _is_daily(body))
            if r.status_code == 400 and ("api key" in low or "api_key" in low):
                raise BadKey("the Gemini API key isn't valid — check it in Settings → AI")
            if r.status_code in (401, 403):
                raise BadKey(f"Gemini refused the API key ({msg[:120]})")
            if r.status_code == 404:
                raise LLMUnavailable(f"Gemini model {model} not found — check the model name in Settings → AI")
            if r.status_code == 400 and "thinking" in low and model not in self._no_thinking:
                self._no_thinking.add(model)
                continue
            if r.status_code == 400 and ("json" in low or "mime" in low) and json_out and model not in self._no_json:
                self._no_json.add(model)
                continue
            if r.status_code == 400 and ("system" in low or "developer instruction" in low) and model not in self._no_system:
                self._no_system.add(model)
                continue
            raise LLMUnavailable(f"Gemini error {r.status_code}: {msg[:200]}")
        raise LLMUnavailable("Gemini kept refusing the request")

    @staticmethod
    def _error(r: httpx.Response) -> dict:
        try:
            data = r.json()
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    @staticmethod
    def _text(data: dict) -> str:
        cands = data.get("candidates") or []
        if not cands:
            reason = (data.get("promptFeedback") or {}).get("blockReason")
            raise ValueError(f"Gemini returned no answer{f' ({reason})' if reason else ''}")
        parts = (cands[0].get("content") or {}).get("parts") or []
        # thought parts (if a model thinks anyway) are not the answer
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))

    def ask(self, model: str, system: str, text: str, image: tuple[bytes, str] | None = None,
            search: bool = False, max_tokens: int = 500) -> str:
        """One plain-text answer. ``image``: (bytes, MIME type) sent with the question (a screenshot);
        ``search``: let the model look things up with Google Search first (live weather, news,
        rates). Both need a Gemini model (Gemma has neither): the API's 400 is raised as is."""
        parts: list[dict[str, Any]] = []
        if image is not None:
            parts.append({"inline_data": {"mime_type": image[1], "data": base64.b64encode(image[0]).decode()}})
        parts.append({"text": text})
        body: dict[str, Any] = {"contents": [{"role": "user", "parts": parts}],
                                "systemInstruction": {"parts": [{"text": system}]},
                                "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_tokens}}
        if search:
            body["tools"] = [{"google_search": {}}]
        r = self._post(model, body)
        if r.status_code == 200:
            return self._text(r.json()).strip()
        err = self._error(r)
        msg = str((err.get("error") or {}).get("message") or r.text[:300])
        if r.status_code == 429:
            raise QuotaExceeded(model, _is_daily(err))
        if r.status_code in (401, 403) or r.status_code == 400 and "api key" in msg.lower():
            raise BadKey(f"Gemini refused the API key ({msg[:120]})")
        raise LLMUnavailable(f"Gemini error {r.status_code}: {msg[:200]}")

    def chat_json(self, model: str, messages: list[dict[str, str]], schema: dict[str, Any] | None = None,
                  temperature: float = 0.2, keep_alive: str = "", num_predict: int | None = None) -> dict[str, Any]:
        text = self.generate(model, messages, json_out=True, temperature=temperature,
                             max_tokens=max(num_predict, 256) if num_predict else 1024)
        return extract_json(text)

    def check_key(self, model: str) -> str:
        """'ok', or why the key/model can't be used (Settings → AI → Test)."""
        try:
            self.generate(model, [{"role": "user", "content": "Reply with the word ok."}], max_tokens=5)
            return "ok"
        except QuotaExceeded as exc:
            return "ok — but " + str(exc)  # the key works; the limit just ran out
        except (LLMUnavailable, ValueError) as exc:
            return str(exc)
