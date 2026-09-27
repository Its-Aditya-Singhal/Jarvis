"""The assistant's reasoning step: command text -> intent -> reply (local LLM).

Keeps a short in-memory context of the last few exchanges so follow-ups
work ("and at 8?"). It is never written to disk (persistent memory is a
later phase) and expires after a few minutes of silence.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .config import Settings
from .database.db import Database
from .llm.client import LLMUnavailable, OllamaClient
from .llm.intents import SCHEMA, Action, Intent, build_messages, compose_reply, detect_language, parse_intent
from .llm.server import OllamaServer

log = logging.getLogger(__name__)

HISTORY_TURNS = 4
HISTORY_TTL_S = 300.0
STATUS_TTL_S = 10.0


@dataclass
class BrainResult:
    reply: str
    language: str
    actions: list[Action]
    latency_s: float
    ok: bool
    chat: str = ""  # the model's own reply (questions/chat); unused for action requests


class Brain:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        names: Callable[[], tuple[str, str]],
        voice_gender: Callable[[], str],
        server: OllamaServer | None = None,
        client: OllamaClient | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.s = settings
        self.db = db
        self.names = names
        self.voice_gender = voice_gender
        self.server = server or OllamaServer(settings.ollama_host, settings.ollama_models_dir, settings.data_dir / "logs")
        self.client = client or OllamaClient(f"http://{settings.ollama_host}", settings.llm_timeout_s)
        self.clock = clock
        self._history: list[tuple[float, str, str, str]] = []  # (t, lang, user, model JSON)
        self._lock = threading.Lock()
        self._status: tuple[float, str] | None = None
        self.installed: list[str] = []

    # -- model selection -------------------------------------------------------
    @property
    def model(self) -> str:
        return self.db.get("llm_model") or self.s.llm_model

    def set_model(self, model: str) -> None:
        if model not in self.installed_models():
            raise ValueError(f"model {model} is not installed")
        self.db.set("llm_model", model)
        self._status = None
        self.clear()

    def installed_models(self) -> list[str]:
        try:
            self.installed = self.client.models()
        except LLMUnavailable:
            self.installed = []
        return self.installed

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> bool:
        if self.s.llm_autostart and not self.server.ensure():
            log.warning("ollama unavailable: %s", self.server.error)
            return False
        if self.model not in self.installed_models():
            return False
        try:
            self.client.warm(self.model)
        except LLMUnavailable:
            return False
        return True

    def stop(self) -> None:
        self.server.stop()

    def status(self) -> str:
        """ready | human-readable reason (cached briefly)."""
        now = time.monotonic()
        if self._status and now - self._status[0] < STATUS_TTL_S:
            return self._status[1]
        try:
            models = self.client.models()
            self.installed = models
            if self.model in models:
                st = "ready"
            else:
                st = f"model {self.model} not installed — run: ollama pull {self.model}"
        except LLMUnavailable:
            st = self.server.error or "Ollama not running"
        self._status = (now, st)
        return st

    # -- reasoning -------------------------------------------------------------
    def clear(self) -> None:
        with self._lock:
            self._history.clear()

    def _messages(self, text: str, lang: str, now: datetime) -> list[dict[str, str]]:
        assistant, owner = self.names()
        cutoff = time.monotonic() - HISTORY_TTL_S
        with self._lock:
            self._history = [h for h in self._history if h[0] >= cutoff][-HISTORY_TURNS:]
            history = [(h[1], h[2], h[3]) for h in self._history]
        return build_messages(assistant, owner, now, lang, text, history)

    @staticmethod
    def _sorry(hindi: bool, gender: str) -> str:
        if hindi:
            return "माफ़ कीजिए, मैं समझ नहीं " + ("पाया।" if gender == "male" else "पाई।")
        return "Sorry, I didn't catch that."

    def respond(self, text: str, stt_lang: str = "en") -> BrainResult:
        t0 = time.monotonic()
        gender = self.voice_gender()
        lang = detect_language(text, stt_lang)
        hindi = lang != "en"
        now = self.clock()
        msgs = self._messages(text, lang, now)
        try:
            try:
                data = self.client.chat_json(self.model, msgs, SCHEMA)
            except ValueError:  # malformed JSON: one retry
                data = self.client.chat_json(self.model, msgs, SCHEMA)
            intent: Intent = parse_intent(data, lang, now)
        except LLMUnavailable as exc:
            self._status = None
            log.warning("llm unavailable: %s", exc)
            reply = (
                "मेरा लोकल लैंग्वेज मॉडल अभी उपलब्ध नहीं है।"
                if hindi
                else "My local language model isn't available right now. Check that Ollama is installed and the model is downloaded."
            )
            return BrainResult(reply, lang, [], time.monotonic() - t0, False)
        except ValueError:
            return BrainResult(self._sorry(hindi, gender), lang, [], time.monotonic() - t0, False)

        reply = compose_reply(intent, gender) or self._sorry(intent.language != "en", gender)
        record = json.dumps(data, ensure_ascii=False)
        with self._lock:
            self._history.append((time.monotonic(), lang, text, record))
        return BrainResult(reply, intent.language, intent.actions, time.monotonic() - t0, True, intent.reply)
