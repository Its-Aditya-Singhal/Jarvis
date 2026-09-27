"""Memory: encrypted facts and history, recall, suggestions, tools and the fast path."""

import hashlib
import time
from datetime import datetime

import numpy as np
import pytest

from jarvis.database.db import Database
from jarvis.llm.client import LLMUnavailable
from jarvis.llm.fastpath import parse_fast
from jarvis.llm.intents import TOOLS, Action, build_messages
from jarvis.memory.manager import Memory
from jarvis.memory.store import MemoryStore
from jarvis.security.crypto import StaticKeyProvider
from jarvis.tools.runner import LEVELS, Plan, ToolRunner
from jarvis.tools.store import ToolStore

SYN = {"birthday": "born", "bday": "born", "drink": "tea", "prefer": "like"}


class FakeClient:
    """Bag-of-words hashing 'embeddings' (with a few synonyms) + scripted extraction."""

    def __init__(self, up=True):
        self.up = up
        self.facts: list[str] = []
        self.chats = 0

    def embed(self, model, texts):
        if not self.up:
            raise LLMUnavailable("model bge-m3 is not installed")
        out = []
        for t in texts:
            v = np.zeros(256)
            for w in t.lower().replace("'s", "").replace("?", "").split():
                w = SYN.get(w, w)
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 256] += 1
            out.append(list(v))
        return out

    def chat_json(self, model, msgs, schema, temperature=0.0):
        self.chats += 1
        return {"facts": self.facts}


@pytest.fixture
def mem(tmp_path):
    db = Database(tmp_path / "m.sqlite")
    client = FakeClient()
    m = Memory(db, MemoryStore(db, StaticKeyProvider()), client,
               extractor=lambda text: client.chat_json("qwen", [], {})["facts"])
    return m, client, db


def test_facts_are_encrypted_at_rest(mem):
    m, _, db = mem
    m.remember("My sister's birthday is 12 March")
    raw = db.run("SELECT text, embedding FROM memories")[0]
    assert b"sister" not in raw["text"] and raw["embedding"] is not None
    assert m.facts()[0].text == "My sister's birthday is 12 March"


def test_recall_finds_the_relevant_fact(mem):
    m, _, _ = mem
    m.remember("My sister Priya's birthday is 12 March")
    m.remember("I prefer tea without sugar")
    m.remember("The wifi password is on the fridge")
    hits = m.search("when is Priya's birthday", min_sim=0.3)
    assert hits and hits[0][1].text.startswith("My sister Priya")
    assert m.relevant("completely unrelated astronomy question") == []


def test_duplicates_refresh_instead_of_piling_up(mem):
    m, _, _ = mem
    f1, created1 = m.remember("I prefer tea without sugar")
    f2, created2 = m.remember("I prefer tea without sugar.")
    assert created1 and not created2 and f1.id == f2.id and len(m.facts()) == 1


def test_word_match_fallback_without_embeddings(tmp_path):
    db = Database(tmp_path / "m.sqlite")
    m = Memory(db, MemoryStore(db, StaticKeyProvider()), FakeClient(up=False))
    m.remember("My car service is on the 5th")
    assert not m.semantic and "not installed" in m.embed_error
    assert m.relevant("when is my car service") == ["My car service is on the 5th"]
    # once the model is available, start() indexes what was stored without vectors
    m.client.up = True
    m.start()
    assert m.semantic and m.facts()[0].embedding is not None


def test_history_is_logged_searchable_and_expires(mem):
    m, _, db = mem
    m.log_turn("what's the bank's phone number", "I don't know that one.", "en")
    m.log_turn("set a timer for 5 minutes", "Timer started for 5 minutes.", "en")
    assert b"bank" not in db.run("SELECT text FROM history LIMIT 1")[0]["text"]
    found = m.search_history("bank phone")
    assert found and found[0][0].text.startswith("what's the bank") and found[0][1].text == "I don't know that one."
    # older than the retention window -> purged
    db.run("UPDATE history SET ts = ?", (time.time() - 40 * 86400,))
    assert m.purge() == 4 and m.history() == []


def test_expired_history_disappears_without_a_restart(mem):
    """The app may run for weeks: history past the retention window must not be shown,
    searched or kept just because nothing restarted the purge."""
    m, _, db = mem
    m.set_retention("7d")
    m.log_turn("what's the bank's phone number", "I don't know that one.", "en")
    db.run("UPDATE history SET ts = ?", (time.time() - 10 * 86400,))  # ten days pass
    assert m.history(days=3650) == []
    assert m.search_history("bank phone") == []
    m._last_purge -= 3700  # an hour later, the next exchange also deletes the old rows
    m.log_turn("hello", "Hi!", "en")
    assert db.run("SELECT COUNT(*) AS n FROM history")[0]["n"] == 2

def test_retention_off_keeps_nothing(mem):
    m, _, _ = mem
    m.log_turn("hello", "hi", "en")
    m.set_retention("off")
    assert m.store.counts()["turns"] == 0
    m.log_turn("hello again", "hi", "en")
    assert m.store.counts()["turns"] == 0
    with pytest.raises(ValueError):
        m.set_retention("1y")


def test_suggestions_only_for_personal_statements(mem):
    m, client, _ = mem
    client.facts = ["The user's exam is on 2 October 2026"]
    assert m.suggest("who wrote hamlet") == [] and client.chats == 0  # not personal: no model call
    sug = m.suggest("my exam is on Friday, I'm nervous")
    assert [s.text for s in sug] == ["The user's exam is on 2 October 2026"]
    assert m.suggest("my exam is on Friday") == []  # already suggested
    assert m.pop_suggestion(sug[0].id).text.startswith("The user's exam") and m.suggestions() == []
    assert Memory.sounds_personal("meri behen ka birthday kal hai")


# -- tools ---------------------------------------------------------------------------------
@pytest.fixture
def runner(mem, tmp_path):
    m, _, db = mem
    r = ToolRunner(db, ToolStore(db, StaticKeyProvider()), None, None, None)
    r.memory = m
    return r, m


def test_memory_tools_levels_and_catalogue():
    assert set(LEVELS) == set(TOOLS)
    assert LEVELS["memory.remember"] == 2 and LEVELS["memory.forget"] == 3 and LEVELS["history.search"] == 1


def test_remember_and_forget_via_tools(runner):
    r, m = runner
    res = r.run(Action("memory.remember", {"text": "my locker code is 4312"}), "en")
    assert res.ok and res.say == "Got it, I'll remember that." and m.facts()[0].text == "My locker code is 4312"
    assert r.run(Action("memory.remember", {"text": "my locker code is 4312"}), "en").say.startswith("I already knew")
    assert not r.run(Action("memory.forget", {"query": "locker"}), "en").ok  # level 3: never runs directly
    plan = r.plan(Action("memory.forget", {"query": "locker code"}), "en")
    assert isinstance(plan, Plan) and "4312" in plan.what
    assert r.execute(plan).ok and m.facts() == []
    miss = r.plan(Action("memory.forget", {"query": "passport"}), "en")
    assert not miss.ok and "don't have anything" in miss.say


def test_history_search_tool(runner):
    r, m = runner
    m.log_turn("what's the bank's phone number", "I don't know that one.", "en")
    res = r.run(Action("history.search", {"query": "bank phone"}), "en")
    assert res.ok and "you said “what's the bank's phone number”" in res.say and "I replied" in res.say


# -- prompt & fast path ---------------------------------------------------------------------
def test_recalled_facts_travel_with_the_user_message():
    msgs = build_messages("Friday", "Aditya", datetime(2026, 9, 27, 10), "en", "when is her birthday", [],
                          ["My sister's birthday is 12 March"])
    assert msgs[-1]["content"].endswith("[Remembered: My sister's birthday is 12 March]\nwhen is her birthday")
    assert "[Remembered" not in msgs[0]["content"].split("A line")[0]  # system prompt stays constant


@pytest.mark.parametrize("text,tool,arg", [
    ("Remember that my sister's birthday is 12 March.", "memory.remember", "My sister's birthday is 12 March"),
    ("yaad rakhna ki meri car ki service 5 tareekh ko hai", "memory.remember", "Meri car ki service 5 tareekh ko hai"),
    ("Forget my sister's birthday", "memory.forget", "My sister's birthday"),
])
def test_fast_path_memory(text, tool, arg):
    r = parse_fast(text, "en", datetime(2026, 9, 27, 10))
    assert r is not None and r.actions[0].tool == tool and list(r.actions[0].args.values())[0] == arg


def test_remember_to_is_a_reminder_not_a_fact():
    assert parse_fast("remember to buy milk", "en", datetime(2026, 9, 27, 10)) is None


def test_relative_dates_are_spelled_out_before_extraction():
    from jarvis.memory.manager import annotate_dates

    now = datetime(2026, 9, 27, 15)  # Sunday
    assert annotate_dates("my exam is on Friday", now) == "my exam is on Friday (Friday 2 October 2026)"
    assert annotate_dates("lunch tomorrow", now) == "lunch tomorrow (Monday 28 September 2026)"
    assert annotate_dates("party day after tomorrow", now) == "party day after tomorrow (Tuesday 29 September 2026)"
    assert annotate_dates("कल मेरा इंटरव्यू है", now).startswith("कल (Monday 28 September 2026)")
    assert annotate_dates("gym on Sunday", now) == "gym on Sunday (Sunday 4 October 2026)"  # next week's
    assert annotate_dates("I like kale", now) == "I like kale"


def test_personal_questions_without_memories_are_marked(settings):
    from jarvis.brain import Brain

    b = Brain(settings, Database(":memory:"), names=lambda: ("Friday", "A"), voice_gender=lambda: "female")
    b.recall = lambda text: []
    msgs = b._messages("what do I like to drink", "en", datetime(2026, 9, 27, 10))
    assert "[Remembered: (nothing relevant remembered)]" in msgs[-1]["content"]
    assert "[Remembered" not in b._messages("who wrote hamlet", "en", datetime(2026, 9, 27, 10))[-1]["content"]
