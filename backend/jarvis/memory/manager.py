"""Long-term memory: remembered facts, conversation history, and recall.

* Facts are saved when the owner says "remember …" (or accepts a suggestion).
* Recall: every LLM request looks up the facts most similar to what was
  said (bge-m3 embeddings through Ollama, multilingual) and passes them to
  the model, so "when is my sister's birthday?" can be answered.
* History: addressed turns only (your words + the reply), kept for the
  owner's chosen retention period and searchable.
* Suggestions: after a chat turn that sounds like a personal fact ("my exam
  is on Friday"), the model is asked in the background whether there is a
  durable fact worth keeping; if so it is offered, never saved silently.

Without the embedding model everything still works, with fuzzy word matching
instead of semantic similarity. Embeddings are filled in later.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from rapidfuzz import fuzz

from ..database.db import Database
from ..llm.client import LLMUnavailable, OllamaClient
from ..speech.text import to_latin
from .store import Fact, MemoryStore, Turn

log = logging.getLogger(__name__)

RETENTION_CHOICES = {"off": 0, "7d": 7, "30d": 30, "forever": None}
DEFAULT_RETENTION = "30d"
PURGE_EVERY_S = 3600.0
RECALL_MIN_SIM = 0.45  # bge-m3 cosine; unrelated facts sit well below
RECALL_K = 4
FUZZY_MIN = 70
MAX_FACT_CHARS = 300
SUGGESTION_TTL_S = 600.0

# a statement about the speaker's own life (cheap filter before asking the model)
_PERSONAL = re.compile(
    r"\b(my|mine|i am|i'm|im|i have|i've|i like|i love|i hate|i prefer|i work|i live|i study|we have|our|"
    r"mera|meri|mere|mujhe|mujhko|main|hum|hamara|hamari|apna|apni)\b"
)

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"facts": {"type": "array", "items": {"type": "string"}}},
    "required": ["facts"],
}
# sent as the last user message after the assistant's usual (cached) prompt prefix
EXTRACT_TASK = """[Task: memory extraction, not a command. Do not answer the message.]
Extract durable personal facts about the user from the message below, for your private memory.
A durable fact stays true for weeks or longer or marks a future date: names of family and friends, birthdays, preferences, allergies, where they work or study, upcoming exams, trips or appointments with a date.
Not facts: requests, questions, feelings of the moment, small talk, anything about the assistant.
An upcoming event with a day or date IS a durable fact, even when mentioned with feelings: "my exam is on Friday, I'm nervous" -> "The user has an exam on Friday 2 October 2026" (use the header's Now to resolve the date).
Write each fact as a short English sentence about "the user", with absolute dates.
Return {"facts": []} when there is nothing worth remembering. At most 2 facts.
Message: """


@dataclass
class Suggestion:
    id: str
    text: str
    created: float  # monotonic


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_HI_WEEKDAYS = {"somvar": 0, "mangalvar": 1, "budhvar": 2, "guruvar": 3, "shukravar": 4, "shanivar": 5, "ravivar": 6}


def annotate_dates(text: str, now: datetime) -> str:
    """Spell out the calendar date after relative day words ("Friday (Friday 2
    October 2026)"), so the model never has to do date arithmetic."""
    from datetime import timedelta

    today = now.date()
    fmt = lambda d: d.strftime("%A %-d %B %Y")
    fixed = {"day after tomorrow": 2, "parso": 2, "parson": 2, "परसों": 2,
             "tomorrow": 1, "kal": 1, "कल": 1, "today": 0, "tonight": 0, "aaj": 0, "आज": 0}
    days = {n: i for i, n in enumerate(_WEEKDAYS)} | _HI_WEEKDAYS
    words = sorted(list(fixed) + list(days), key=len, reverse=True)
    pattern = re.compile(r"(?<![\w\u0900-\u097F])(" + "|".join(map(re.escape, words)) + r")(?![\w\u0900-\u097F])", re.I)

    def repl(m: re.Match) -> str:
        w = m.group(1).lower()
        if w in fixed:
            d = today + timedelta(days=fixed[w])
        else:
            d = today + timedelta(days=(days[w] - today.weekday()) % 7 or 7)  # "Friday" said on a Friday = next week's
        return f"{m.group(1)} ({fmt(d)})"

    return pattern.sub(repl, text)


def _norm(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class Memory:
    def __init__(
        self,
        db: Database,
        store: MemoryStore,
        client: OllamaClient | None,
        embed_model: str = "bge-m3",
        extractor: Callable[[str], list[str]] | None = None,
        on_change: Callable[[], None] = lambda: None,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.db = db
        self.store = store
        self.client = client
        self.embed_model = embed_model
        self.extractor = extractor  # the brain's fact extraction (shares its cached prompt)
        self.on_change = on_change
        self.clock = clock
        self.embed_error: str | None = "not checked yet"
        self._lock = threading.Lock()
        self._last_purge = time.monotonic()
        self._facts: list[Fact] | None = None  # decrypted cache
        self._suggestions: list[Suggestion] = []

    # -- embeddings ---------------------------------------------------------------
    def embed(self, texts: list[str]) -> list[np.ndarray] | None:
        if not self.embed_model:
            self.embed_error = "off (word matching keeps JARVIS light)"
            return None
        if self.client is None or not texts:
            return None
        try:
            vecs = self.client.embed(self.embed_model, texts)
            self.embed_error = None
            return [_norm(np.array(v)) for v in vecs]
        except LLMUnavailable as exc:
            self.embed_error = str(exc)
            return None

    @property
    def semantic(self) -> bool:
        return self.embed_error is None

    def start(self) -> None:
        """Purge expired history and index remembered facts stored without an embedding."""
        self.purge()
        missing = [f for f in self.facts() if f.embedding is None]
        if not missing:
            return  # nothing to index: don't load the recall model at launch
        if self.embed(["warm up"]) is None:
            log.info("memory recall without embeddings: %s", self.embed_error)
            return
        for f in missing:
            if f.embedding is None and (v := self.embed([f.text])):
                self.store.set_fact_embedding(f.id, v[0])
        self._invalidate()

    # -- facts --------------------------------------------------------------------
    def _invalidate(self) -> None:
        with self._lock:
            self._facts = None

    def facts(self) -> list[Fact]:
        with self._lock:
            if self._facts is None:
                self._facts = self.store.facts()
            return list(self._facts)

    @staticmethod
    def clean(text: str) -> str:
        text = " ".join(str(text or "").split())[:MAX_FACT_CHARS].strip(" .")
        return text[:1].upper() + text[1:] if text else ""

    def remember(self, text: str, source: str = "said") -> tuple[Fact | None, bool]:
        """Returns (fact, created). A near-duplicate refreshes the existing fact."""
        text = self.clean(text)
        if not text:
            return None, False
        vec = self.embed([text])
        v = vec[0] if vec else None
        dup = self._duplicate(text, v)
        if dup is not None:
            self.store.update_fact(dup.id, text, v if v is not None else dup.embedding)
            self._invalidate()
            self.on_change()
            return next((f for f in self.facts() if f.id == dup.id), dup), False
        fid = self.store.add_fact(text, source, v)
        self._invalidate()
        self.on_change()
        return next(f for f in self.facts() if f.id == fid), True

    def _duplicate(self, text: str, v: np.ndarray | None) -> Fact | None:
        for f in self.facts():
            if v is not None and f.embedding is not None and float(f.embedding @ v) > 0.93:
                return f
            if fuzz.ratio(text.lower(), f.text.lower()) > 92:
                return f
        return None

    def update(self, fact_id: int, text: str) -> bool:
        text = self.clean(text)
        if not text:
            return False
        vec = self.embed([text])
        ok = self.store.update_fact(fact_id, text, vec[0] if vec else None)
        self._invalidate()
        self.on_change()
        return ok

    def delete(self, fact_id: int) -> bool:
        ok = self.store.delete_fact(fact_id)
        self._invalidate()
        self.on_change()
        return ok

    def search(self, query: str, k: int = RECALL_K, min_sim: float = RECALL_MIN_SIM) -> list[tuple[float, Fact]]:
        """Facts relevant to ``query``, best first, as (score 0-1, fact)."""
        facts = self.facts()
        if not facts or not query.strip():
            return []
        vec = self.embed([query]) if any(f.embedding is not None for f in facts) else None
        q = vec[0] if vec is not None else None
        ql = to_latin(query)
        scored: list[tuple[float, Fact]] = []
        for f in facts:
            if q is not None and f.embedding is not None and len(f.embedding) == len(q):
                s = float(f.embedding @ q)
                if s >= min_sim:
                    scored.append((s, f))
            else:
                # not indexed yet (saved while the embedding model was down) or no embeddings at all:
                # word matching, so the fact isn't invisible until the next restart
                s = fuzz.token_set_ratio(ql, to_latin(f.text)) / 100
                if s * 100 >= FUZZY_MIN:
                    scored.append((s, f))
        return sorted(scored, key=lambda x: -x[0])[:k]

    def relevant(self, query: str) -> list[str]:
        return [f.text for _, f in self.search(query)]

    # -- history ------------------------------------------------------------------
    @property
    def retention(self) -> str:
        r = self.db.get("history_retention", DEFAULT_RETENTION)
        return r if r in RETENTION_CHOICES else DEFAULT_RETENTION

    def set_retention(self, value: str) -> None:
        if value not in RETENTION_CHOICES:
            raise ValueError(f"retention must be one of {', '.join(RETENTION_CHOICES)}")
        self.db.set("history_retention", value)
        self.purge()

    def _cutoff(self) -> float | None:
        """Oldest time history may be from under the owner's retention setting (None: forever)."""
        days = RETENTION_CHOICES[self.retention]
        return None if days is None else time.time() - days * 86400

    def _maybe_purge(self) -> None:
        # the app may run for weeks: expired history is deleted hourly, not only at startup
        if time.monotonic() - self._last_purge >= PURGE_EVERY_S:
            self.purge()

    def purge(self) -> int:
        self._last_purge = time.monotonic()
        days = RETENTION_CHOICES[self.retention]
        if days is None:
            return 0
        n = self.store.delete_turns_before(time.time() - days * 86400) if days else self.store.clear_history()
        if n:
            self.on_change()
        return n

    def log_turn(self, you: str, reply: str, lang: str, embed: bool = True) -> None:
        """Keep one exchange (text only). Called after the reply is sent. ``embed`` False (an
        everyday command): stored without an embedding, so the recall model isn't loaded just
        for "turn the volume up"; history search finds it by its words."""
        if self.retention == "off" or not you.strip():
            return
        self._maybe_purge()
        vec = self.embed([you]) if embed else None
        self.store.add_turn("you", you, lang, vec[0] if vec else None)
        if reply.strip():
            self.store.add_turn("assistant", reply, lang)
        self.on_change()

    def history(self, days: int = 30, limit: int = 400) -> list[Turn]:
        since = time.time() - days * 86400
        cutoff = self._cutoff()
        return self.store.turns(since=since if cutoff is None else max(since, cutoff), limit=limit)

    def search_history(self, query: str, day: datetime | None = None, k: int = 3) -> list[tuple[Turn, Turn | None]]:
        """Past exchanges matching ``query`` (optionally on one day): (your turn, the reply)."""
        cutoff = self._cutoff() or 0.0
        if day is not None:
            d0 = datetime.combine(day.date(), datetime.min.time()).timestamp()
            turns = self.store.turns(since=max(d0, cutoff), until=d0 + 86400)
        else:
            turns = self.store.turns(since=cutoff, limit=2000)
        mine = [t for t in turns if t.role == "you"]
        scored: list[tuple[float, Turn]] = []
        vec = self.embed([query]) if query.strip() else None
        for t in mine:
            if not query.strip():
                s = 1.0
            elif vec is not None and t.embedding is not None and len(t.embedding) == len(vec[0]):
                s = float(t.embedding @ vec[0])
                s = s if s >= RECALL_MIN_SIM else 0.0
            else:
                s = fuzz.token_set_ratio(to_latin(query), to_latin(t.text)) / 100
                s = s if s * 100 >= FUZZY_MIN else 0.0
            if s > 0:
                scored.append((s, t))
        best = sorted(scored, key=lambda x: (-x[0], -x[1].ts.timestamp()))[:k]
        by_time = sorted(turns, key=lambda t: t.ts)
        out = []
        for _, t in best:
            later = [u for u in by_time if u.role == "assistant" and u.ts >= t.ts]
            out.append((t, later[0] if later and (later[0].ts - t.ts).total_seconds() < 120 else None))
        return out

    def clear_history(self) -> int:
        n = self.store.clear_history()
        self.on_change()
        return n

    def clear_facts(self) -> int:
        n = self.store.clear_facts()
        self._invalidate()
        self.on_change()
        return n

    def reset_cache(self) -> None:
        """After a factory reset: drop decrypted facts and pending suggestions."""
        self._invalidate()
        with self._lock:
            self._suggestions = []
        self.on_change()

    # -- suggestions ----------------------------------------------------------------
    def suggestions(self) -> list[Suggestion]:
        cutoff = time.monotonic() - SUGGESTION_TTL_S
        with self._lock:
            self._suggestions = [s for s in self._suggestions if s.created >= cutoff]
            return list(self._suggestions)

    def pop_suggestion(self, sid: str) -> Suggestion | None:
        with self._lock:
            for s in self._suggestions:
                if s.id == sid:
                    self._suggestions.remove(s)
                    return s
        return None

    @staticmethod
    def sounds_personal(text: str) -> bool:
        return bool(_PERSONAL.search(to_latin(text)))

    def suggest(self, text: str) -> list[Suggestion]:
        """Ask the model for durable facts in ``text`` (blocking; run in the background)."""
        if self.extractor is None or not self.sounds_personal(text):
            return []
        try:
            raw_facts = self.extractor(annotate_dates(text[:500], self.clock()))
        except (LLMUnavailable, ValueError):
            return []
        new: list[Suggestion] = []
        known = [f.text.lower() for f in self.facts()] + [s.text.lower() for s in self.suggestions()]
        for raw in raw_facts[:2]:
            fact = self.clean(raw)
            if len(fact) < 8 or any(fuzz.ratio(fact.lower(), k) > 85 for k in known):
                continue
            s = Suggestion(secrets.token_hex(4), fact, time.monotonic())
            new.append(s)
            known.append(fact.lower())
        if new:
            with self._lock:
                self._suggestions = (self._suggestions + new)[-6:]
        return new

    # -- summary ----------------------------------------------------------------------
    def status(self) -> dict:
        return {
            **self.store.counts(),
            "retention": self.retention,
            "recall": "semantic (bge-m3)" if self.semantic else f"word match — {self.embed_error}",
        }
