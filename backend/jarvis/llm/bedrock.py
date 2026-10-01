"""Claude through Amazon Bedrock: an optional, paid brain (the owner's AWS credits pay for it).

Off unless the owner picks it in Settings → AI; the free Gemini API stays the default and, when a
Gemini key is saved, answers whenever Bedrock can't (throttled, access not enabled, key expired).

Bedrock serves Claude through the Messages API at
``https://bedrock-mantle.{region}.api.aws/anthropic``; a Bedrock API key goes in as the bearer
token, so there are no AWS access keys or request signing on the Mac. Any Claude model the account
can use works: one for commands (Haiku by default) and one for reading and writing (Sonnet by
default), both settings.

Models differ in what they accept (thinking off, sampling temperature): an option a model refuses
is dropped after its first 400 and remembered, never assumed from the name. Bedrock has no
structured outputs, so the JSON reply is asked for in the prompt and read with ``extract_json``.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2  # the SDK's HTTP layer (an httpx fork); transports handed to it must come from here

from .client import LLMUnavailable
from .gemini import BadKey, QuotaExceeded, extract_json

log = logging.getLogger(__name__)

BEDROCK_KEY = "bedrock_api_key"  # its name in the sealed secrets store
REGION = "us-east-1"
FAST_MODEL = "anthropic.claude-haiku-4-5"
HEAVY_MODEL = "anthropic.claude-sonnet-5"
# suggestions for the Settings lists; any other model id the account can use works too
MODELS = [
    "anthropic.claude-haiku-4-5",
    "anthropic.claude-sonnet-5",
    "anthropic.claude-sonnet-5-5",
    "anthropic.claude-opus-4-7",
    "anthropic.claude-opus-4-8",
    "anthropic.claude-opus-5",
    "anthropic.claude-opus-5-5",
    "anthropic.claude-fable-5",
    "anthropic.claude-fable-5-1",
]
KEY_PAGE = "https://console.aws.amazon.com/bedrock/home#/api-keys"
ACCESS_PAGE = "https://console.aws.amazon.com/bedrock/home#/modelaccess"
MODEL_NAME = r"^[a-z0-9][a-z0-9.:\-]{1,99}$"
REGION_NAME = r"^[a-z]{2}(-[a-z]+)+-\d$"
KEY_CHARS = re.compile(r"^[A-Za-z0-9+/=._\-]{20,4000}$")
JSON_ONLY = "\n\nAnswer with one JSON object only: no prose, no code fences."

# what to send for "no deliberation, just answer", in the order models accept it
THINKING = ("disabled", "between_tools", "low_effort", "default")


def host(region: str) -> str:
    return f"bedrock-mantle.{region}.api.aws"


class BedrockClient:
    def __init__(self, api_key: Callable[[], str | None], region: Callable[[], str] = lambda: REGION,
                 timeout_s: float = 30.0, transport: httpx2.BaseTransport | None = None):
        self.api_key = api_key  # read on every call: the owner can change it in Settings
        self.region = region
        self._timeout = timeout_s
        self._transport = transport
        self._lock = threading.Lock()
        self._sdk: tuple[tuple[str, str], anthropic.Anthropic] | None = None
        self._thinking: dict[str, int] = {}  # model -> index into THINKING that it accepts
        self._no_temperature: set[str] = set()

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
    def _client(self) -> anthropic.Anthropic:
        key = (self.api_key() or "").strip()
        if not key:
            raise BadKey("no Bedrock API key — add one in Settings → AI")
        region = self.region() or REGION
        with self._lock:
            if self._sdk is None or self._sdk[0] != (key, region):
                # no SDK retries: the brain moves on to the next model (or Gemini) at once instead
                http = anthropic.DefaultHttpxClient(transport=self._transport) if self._transport else None
                sdk = anthropic.Anthropic(api_key=key, base_url=f"https://{host(region)}/anthropic",
                                          timeout=anthropic.Timeout(self._timeout, connect=5.0), max_retries=0,
                                          http_client=http)
                self._sdk = ((key, region), sdk)
            return self._sdk[1]

    def _params(self, model: str, messages: list[dict[str, str]], temperature: float,
                max_tokens: int) -> dict[str, Any]:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        msgs = [{"role": "assistant" if m["role"] == "assistant" else "user", "content": m["content"]}
                for m in messages if m["role"] != "system"]
        p: dict[str, Any] = {"model": model, "messages": msgs, "max_tokens": max_tokens}
        if system:
            p["system"] = system
        extra: dict[str, Any] = {}
        mode = THINKING[self._thinking.get(model, 0)]
        if mode in ("disabled", "between_tools"):
            extra["thinking"] = {"type": mode}
        else:  # a model that always thinks: keep it short, and leave room for the answer itself
            if mode == "low_effort":
                extra["output_config"] = {"effort": "low"}
            p["max_tokens"] = max(max_tokens, 4096)
        if model not in self._no_temperature and mode in ("disabled", "between_tools"):
            extra["temperature"] = temperature  # in the body as is: newer SDKs drop sampling arguments
        if extra:
            p["extra_body"] = extra
        return p

    def _drop(self, model: str, msg: str) -> bool:
        """Learn from a 400 which option the model refuses; False when it isn't one we can drop."""
        low = msg.lower()
        if ("temperature" in low or "sampling" in low) and model not in self._no_temperature:
            self._no_temperature.add(model)
            return True
        if "thinking" in low or "effort" in low:
            i = self._thinking.get(model, 0)
            if i + 1 < len(THINKING):
                self._thinking[model] = i + 1
                return True
        return False

    def generate(self, model: str, messages: list[dict[str, str]], json_out: bool = False,
                 temperature: float = 0.2, max_tokens: int | None = None) -> str:
        sdk = self._client()
        region = self.region() or REGION
        if json_out:
            messages = [*messages]
            sys_at = next((i for i, m in enumerate(messages) if m["role"] == "system"), None)
            if sys_at is None:
                messages.insert(0, {"role": "system", "content": JSON_ONLY.strip()})
            else:
                messages[sys_at] = {**messages[sys_at], "content": messages[sys_at]["content"] + JSON_ONLY}
        for _ in range(len(THINKING) + 2):  # at most one retry per refused option
            try:
                r = sdk.messages.create(**self._params(model, messages, temperature, max_tokens or 1024))
            except anthropic.BadRequestError as exc:
                msg = _message(exc)
                if self._drop(model, msg):
                    continue
                if "throughput" in msg.lower() or "inference profile" in msg.lower():
                    raise LLMUnavailable(f"{model} needs a different model id in {region} — check it in Settings → AI") from exc
                raise LLMUnavailable(f"Bedrock refused the request: {msg[:200]}") from exc
            except anthropic.AuthenticationError as exc:
                raise BadKey("the Bedrock API key isn't valid or has expired (short-term keys last 12 hours) — "
                             "paste a new one in Settings → AI") from exc
            except anthropic.PermissionDeniedError as exc:
                raise BadKey(f"your AWS account can't use {model} in {region} yet — turn on access to it in the "
                             f"Bedrock console (Model access), or check the key's permissions") from exc
            except anthropic.NotFoundError as exc:
                raise LLMUnavailable(f"{model} isn't available in {region} — check the model name and region "
                                     "in Settings → AI") from exc
            except anthropic.RateLimitError as exc:
                raise QuotaExceeded(model, daily=False, provider="bedrock") from exc
            except anthropic.APIStatusError as exc:
                raise LLMUnavailable(f"Bedrock error {exc.status_code}: {_message(exc)[:200]}") from exc
            except anthropic.APIConnectionError as exc:
                raise LLMUnavailable(f"Bedrock not reachable: {exc.__class__.__name__}") from exc
            if r.stop_reason == "refusal":
                raise ValueError(f"{model} declined to answer")
            return "".join(b.text for b in r.content if b.type == "text")
        raise LLMUnavailable("Bedrock kept refusing the request")

    def chat_json(self, model: str, messages: list[dict[str, str]], schema: dict[str, Any] | None = None,
                  temperature: float = 0.2, keep_alive: str = "", num_predict: int | None = None) -> dict[str, Any]:
        text = self.generate(model, messages, json_out=True, temperature=temperature,
                             max_tokens=max(num_predict, 256) if num_predict else 1024)
        return extract_json(text)

    def check_key(self, model: str) -> str:
        """'ok', or why the key/model/region can't be used (Settings → AI → Test)."""
        try:
            self.generate(model, [{"role": "user", "content": "Reply with the word ok."}], max_tokens=16)
            return "ok"
        except QuotaExceeded:
            return "ok — but Bedrock is throttling requests right now"
        except (LLMUnavailable, ValueError) as exc:
            return str(exc)


def _message(exc: anthropic.APIStatusError) -> str:
    body = exc.body if isinstance(exc.body, dict) else {}
    err = body.get("error")
    return str((err if isinstance(err, dict) else body).get("message") or exc.message or "")
