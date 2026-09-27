"""The command-routing corpus (tests/corpus/routing.txt) against the fast path, and,
where Ollama is available, against the language model's prompt."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from jarvis.fakes import fake_apps_dir
from jarvis.llm.fastpath import parse_fast
from jarvis.llm.intents import TOOLS, detect_language
from jarvis.tools.apps import AppIndex

CORPUS = Path(__file__).parent / "corpus" / "routing.txt"
NOW = datetime(2026, 9, 27, 16, 30)  # Sunday
APPS = ("Safari", "Notes", "Calendar", "Music", "Photos", "Mail", "Messages", "Terminal", "System Settings",
        "Calculator", "Spotify", "WhatsApp", "Visual Studio Code", "Google Chrome", "Slack", "Xcode", "Preview")


@dataclass
class Case:
    line: int
    route: str  # fast | llm | gap
    expected: str
    text: str

    @property
    def id(self) -> str:
        return f"{self.line}:{self.text[:40]}"


def load() -> list[Case]:
    cases = []
    for n, raw in enumerate(CORPUS.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        route, expected, text = (p.strip() for p in raw.split("|", 2))
        assert route in ("fast", "llm", "gap"), f"line {n}: unknown route {route!r}"
        cases.append(Case(n, route, expected, text))
    return cases


CASES = load()


def _value(v: str):
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return {"true": True, "false": False}.get(v, v)


def expected_actions(spec: str) -> tuple[list[tuple[str, dict]], str | None]:
    """'tool(k=v, ...) + tool()' -> actions; 'reply=time' -> ([], 'time'); 'none' -> ([], None)."""
    if spec == "none":
        return [], None
    if spec.startswith("reply="):
        return [], spec.split("=", 1)[1]
    out = []
    for part in spec.split(" + "):
        m = re.fullmatch(r"([\w.]+)(?:\((.*)\))?", part.strip())
        assert m, f"bad expectation {part!r}"
        args = {}
        for kv in filter(None, (s.strip() for s in (m.group(2) or "").split(","))):
            k, v = kv.split("=", 1)
            args[k.strip()] = _value(v.strip())
        out.append((m.group(1), args))
    return out, None


@pytest.fixture(scope="module")
def is_app(tmp_path_factory):
    apps = AppIndex([fake_apps_dir(tmp_path_factory.mktemp("mac"), APPS)])
    return lambda name: apps.resolve(name) is not None


def fast(text: str, is_app):
    return parse_fast(text, detect_language(text), NOW, is_app)


def _norm(args: dict) -> dict:
    return {k: v.lower() if isinstance(v, str) and k == "name" else v for k, v in args.items()}


def check_fast(case: Case, is_app) -> None:
    want, reply = expected_actions(case.expected)
    r = fast(case.text, is_app)
    assert r is not None, f"not understood instantly: {case.text!r}"
    assert [(a.tool, _norm(a.args)) for a in r.actions] == [(t, _norm(a)) for t, a in want]
    if reply == "time":
        assert r.reply in ("It's 4:30 PM.", "अभी शाम 4:30 बजे हैं।") or "4:30" in r.reply
    elif reply == "date":
        assert "27" in r.reply and ("September" in r.reply or "सितंबर" in r.reply)
    elif reply is not None:  # reply=<words the answer must contain>
        assert reply in r.reply, r.reply
    else:
        assert r.reply == ""


def test_corpus_is_well_formed():
    assert len(CASES) >= 300
    for c in CASES:
        want, _ = expected_actions(c.expected)
        for tool, _args in want:
            assert tool in TOOLS, f"line {c.line}: unknown tool {tool}"
    texts = [c.text for c in CASES]  # case variants are deliberate
    dupes = {t for t in texts if texts.count(t) > 1}
    assert not dupes, f"duplicate phrasings: {sorted(dupes)}"


@pytest.mark.parametrize("case", [c for c in CASES if c.route == "fast"], ids=lambda c: c.id)
def test_fast_path_understands(case, is_app):
    check_fast(case, is_app)


@pytest.mark.parametrize("case", [c for c in CASES if c.route == "gap"], ids=lambda c: c.id)
@pytest.mark.xfail(strict=True, reason="known gap in the fast path: change the line to 'fast' once fixed")
def test_known_fast_path_gaps(case, is_app):
    check_fast(case, is_app)


@pytest.mark.parametrize("case", [c for c in CASES if c.route == "llm"], ids=lambda c: c.id)
def test_the_rest_goes_to_the_language_model(case, is_app):
    assert fast(case.text, is_app) is None, "the fast path must not guess this one"


# -- the language model (Mac / wherever Ollama runs) ------------------------------------------
@pytest.fixture(scope="module")
def brain(tmp_path_factory):
    from test_llm_live import KV

    from jarvis.brain import Brain
    from jarvis.config import Settings
    from jarvis.llm.client import LLMUnavailable, OllamaClient
    from jarvis.llm.server import OllamaServer

    s = Settings(data_dir=tmp_path_factory.mktemp("llm"))
    client = OllamaClient(f"http://{s.ollama_host}")
    try:
        if s.llm_model not in client.models():
            pytest.skip(f"{s.llm_model} not pulled")
    except LLMUnavailable:
        pytest.skip("Ollama not running")
    return Brain(s, KV(), lambda: ("Jarvis", "Aditya"), lambda: "female",
                 server=OllamaServer(s.ollama_host, None, s.data_dir), client=client, clock=lambda: NOW)


@pytest.mark.ollama
@pytest.mark.parametrize("case", [c for c in CASES if c.route == "llm"], ids=lambda c: c.id)
def test_language_model_routes(case, brain):
    brain.clear()
    r = brain.respond(case.text)
    want, _ = expected_actions(case.expected)
    assert r.ok and [a.tool for a in r.actions] == [t for t, _ in want]
