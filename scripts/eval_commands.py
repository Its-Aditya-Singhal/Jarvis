"""Run every command in the routing corpus through JARVIS's real understanding path with the
local models, and report the pass rate. Nothing is executed: only what JARVIS would do is
compared with what the corpus expects. Needs Ollama with the models pulled.

    backend/.venv/bin/python scripts/eval_commands.py                        # qwen2.5:7b and qwen2.5:3b
    backend/.venv/bin/python scripts/eval_commands.py --models qwen2.5:3b --model-only
    backend/.venv/bin/python scripts/eval_commands.py --scripts             # + write an AppleScript for each mac.do line

Default: the path the owner uses (instant patterns first, then the model).
--model-only: every command goes to the model, to measure the model itself.
--scripts: for each "mac.do" line, the model writes an AppleScript, which is compiled with
osacompile (never run) and classified; blocked or uncompilable scripts count as failures,
except on lines that expect no action. Each model runs with its own Mac profile's context
(qwen2.5:3b as on an 8 GB Mac).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests"))

from test_routing_corpus import CASES, NOW, expected_actions  # noqa: E402


class KV:
    def __init__(self): self.kv: dict = {}
    def get(self, k, d=None): return self.kv.get(k, d)
    def set(self, k, v): self.kv[k] = v


def run(model: str, model_only: bool, scripts: bool, only: str | None) -> tuple[int, int, list[str]]:
    # the context each model really gets: 3B is what an 8 GB Mac runs
    os.environ["JARVIS_RAM_GB"] = "8" if "3b" in model else "16"
    from jarvis import hardware

    hardware.profile.cache_clear()
    import jarvis.brain as brain_mod
    from jarvis.config import Settings
    from jarvis.llm.client import OllamaClient
    from jarvis.llm.server import OllamaServer
    from jarvis.tools.agent import ScriptAgent, compile_check
    from jarvis.tools.apps import AppIndex

    s = Settings(data_dir=Path("/tmp/jarvis-eval"))
    client = OllamaClient(f"http://{s.ollama_host}", 180.0)
    if model not in client.models():
        sys.exit(f"{model} isn't pulled: ollama pull {model}")
    kv = KV()
    kv.set("llm_model", model)
    b = brain_mod.Brain(s, kv, lambda: ("Jarvis", "Aditya"), lambda: "female",
                        server=OllamaServer(s.ollama_host, None, s.data_dir), client=client, clock=lambda: NOW)
    apps = AppIndex([Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications"])
    b.is_app = lambda name: apps.resolve(name) is not None
    real_fast = brain_mod.parse_fast
    if model_only:
        brain_mod.parse_fast = lambda *a, **k: None
    agent = ScriptAgent(lambda msgs, schema: client.chat_json(model, msgs, schema, temperature=0.1, num_predict=700),
                        compiler=compile_check, runner=lambda script: "")
    try:
        client.warm(model)
        passed, total, failures = 0, 0, []
        for c in CASES:
            if only and only.lower() not in c.text.lower():
                continue
            want, _ = expected_actions(c.expected)
            b.clear()
            t0 = time.monotonic()
            r = b.respond(c.text)
            got = [a.tool for a in r.actions]
            ok = r.ok and got == [t for t, _ in want]
            note = ""
            if ok and scripts:
                for a in r.actions:
                    if a.tool != "mac.do":
                        continue
                    try:
                        sc = agent.write(str(a.args.get("task") or ""))
                        note += f" [{sc.verdict}{': ' + sc.reason if sc.reason else ''}] {sc.summary}"
                        ok = ok and sc.verdict != "blocked"
                    except Exception as exc:  # any failure to write a script counts
                        note += f" [no script: {exc}]"
                        ok = False
            total += 1
            passed += ok
            mark = "ok " if ok else "BAD"
            print(f"{mark} {time.monotonic() - t0:5.1f}s  {c.text!r} -> {got or r.reply[:60]!r}{note}", flush=True)
            if not ok:
                failures.append(f"line {c.line}: {c.text!r} expected {c.expected}, got {got or r.reply!r}{note}")
        return passed, total, failures
    finally:
        brain_mod.parse_fast = real_fast


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="+", default=["qwen2.5:7b", "qwen2.5:3b"])
    ap.add_argument("--model-only", action="store_true", help="skip the instant patterns: test the model itself")
    ap.add_argument("--scripts", action="store_true", help="also write and compile an AppleScript for each mac.do")
    ap.add_argument("--only", help="only commands containing these words")
    args = ap.parse_args()
    summary = []
    for m in args.models:
        print(f"\n=== {m} ===")
        p, n, failures = run(m, args.model_only, args.scripts, args.only)
        summary.append(f"{m}: {p}/{n} ({100 * p / max(n, 1):.0f}%)")
        if failures:
            print("\nFailures:\n  " + "\n  ".join(failures))
    print("\n" + "\n".join(summary))


if __name__ == "__main__":
    main()
