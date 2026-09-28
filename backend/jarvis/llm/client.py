"""Minimal Ollama HTTP client (chat with JSON-schema structured output)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..hardware import profile


class LLMUnavailable(RuntimeError):
    pass


CHAT_KEEP_ALIVE = profile().chat_keep_alive
EMBED_KEEP_ALIVE = profile().embed_keep_alive


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
        keep_alive: str = CHAT_KEEP_ALIVE,
        num_predict: int | None = None,
    ) -> dict[str, Any]:
        body = {
            "model": model,
            "messages": messages,
            "format": schema,
            "stream": False,
            "keep_alive": keep_alive,
            "options": {"temperature": temperature, "num_ctx": profile().num_ctx,
                        **({"num_predict": num_predict} if num_predict else {})},
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

    def embed(self, model: str, texts: list[str], keep_alive: str = EMBED_KEEP_ALIVE) -> list[list[float]]:
        """Sentence embeddings (e.g. bge-m3), one vector per text."""
        try:
            r = self._http.post(f"{self.url}/api/embed", json={"model": model, "input": texts, "keep_alive": keep_alive})
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"Ollama not reachable: {exc}") from exc
        if r.status_code == 404:
            raise LLMUnavailable(f"model {model} is not installed")
        if r.status_code >= 400:
            raise LLMUnavailable(f"Ollama error {r.status_code}: {r.text[:200]}")
        vecs = r.json().get("embeddings") or []
        if len(vecs) != len(texts):
            raise LLMUnavailable("embedding count mismatch")
        return vecs

    def warm(self, model: str) -> None:
        """Load the model into memory so the first command isn't slow."""
        try:
            self._http.post(f"{self.url}/api/generate", json={"model": model, "prompt": "", "keep_alive": CHAT_KEEP_ALIVE})
        except httpx.HTTPError as exc:
            raise LLMUnavailable(str(exc)) from exc

    def loaded(self) -> dict[str, int]:
        """Models Ollama holds in memory -> bytes."""
        try:
            r = self._http.get(f"{self.url}/api/ps")
            r.raise_for_status()
        except httpx.HTTPError:
            return {}
        return {m["name"]: int(m.get("size") or 0) for m in r.json().get("models", [])}

    def pull(self, model: str, on_progress=lambda status, completed, total: None) -> None:
        """Download a model into Ollama (it resumes interrupted pulls itself), reporting progress."""
        try:
            with self._http.stream("POST", f"{self.url}/api/pull", json={"model": model, "stream": True},
                                   timeout=httpx.Timeout(None, connect=2.0)) as r:
                if r.status_code >= 400:
                    r.read()
                    raise LLMUnavailable(f"Ollama error {r.status_code}: {r.text[:200]}")
                for line in r.iter_lines():
                    if not line.strip():
                        continue
                    msg = json.loads(line)
                    if msg.get("error"):
                        raise LLMUnavailable(str(msg["error"]))
                    on_progress(str(msg.get("status", "")), int(msg.get("completed") or 0), int(msg.get("total") or 0))
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"Ollama not reachable: {exc}") from exc

    def unload(self, model: str) -> None:
        """Free a model's memory now (it reloads on next use)."""
        try:
            self._http.post(f"{self.url}/api/generate", json={"model": model, "keep_alive": 0}, timeout=10.0)
        except httpx.HTTPError:
            pass
