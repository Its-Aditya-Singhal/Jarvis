"""Minimal Ollama HTTP client (chat with JSON-schema structured output)."""

from __future__ import annotations

import json
from typing import Any

import httpx


class LLMUnavailable(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, url: str, timeout_s: float = 90.0, transport: httpx.BaseTransport | None = None):
        self.url = url.rstrip("/")
        self._http = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=2.0), transport=transport)

    def models(self) -> list[str]:
        try:
            r = self._http.get(f"{self.url}/api/tags")
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"Ollama not reachable: {exc}") from exc
        return sorted(m["name"] for m in r.json().get("models", []))

    def chat_json(
        self,
        model: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        temperature: float = 0.2,
        keep_alive: str = "30m",
    ) -> dict[str, Any]:
        body = {
            "model": model,
            "messages": messages,
            "format": schema,
            "stream": False,
            "keep_alive": keep_alive,
            "options": {"temperature": temperature, "num_ctx": 4096},
        }
        try:
            r = self._http.post(f"{self.url}/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"Ollama not reachable: {exc}") from exc
        if r.status_code == 404:
            raise LLMUnavailable(f"model {model} is not installed")
        if r.status_code >= 400:
            raise LLMUnavailable(f"Ollama error {r.status_code}: {r.text[:200]}")
        content = r.json().get("message", {}).get("content", "")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"model returned invalid JSON: {content[:200]}") from exc

    def warm(self, model: str) -> None:
        """Load the model into memory so the first command isn't slow."""
        try:
            self._http.post(f"{self.url}/api/generate", json={"model": model, "prompt": "", "keep_alive": "30m"})
        except httpx.HTTPError as exc:
            raise LLMUnavailable(str(exc)) from exc
