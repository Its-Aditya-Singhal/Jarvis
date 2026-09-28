"""Any command: the local model writes an AppleScript for requests no built-in tool covers.

The owner chose this design for "do anything" requests (``mac.do``):

1. The language model writes one AppleScript for the task (``ScriptAgent.write``), with a
   one-line summary. The script is compiled (``osacompile``) before anyone sees it; a
   compile error goes back to the model once.
2. Code, not the model, classifies the script (``classify``):
   * blocked: anything that escapes AppleScript's app dictionaries or is destructive or
     secret: shell commands, Terminal, other scripts, the Objective-C bridge, raw «event»
     codes, JavaScript in web pages, file writing, deleting files, emptying the Trash,
     passwords and the keychain, admin rights. These never run, whatever anyone confirms.
   * read: only asks apps for information. Runs straight away.
   * change: everything else. Shown to the owner with the full script; runs only after
     they confirm (the same level-3 confirmation as a deletion).
   The model's own claim that a script "only reads" is never trusted on its own: both the
   model and the classifier must agree.
3. On an 8 GB Mac (the small 3B model), or when the owner picks "Always ask" in Settings,
   every generated script needs confirmation, reads included.

Scripts run through ``osascript`` with the script on stdin (no shell), a time limit and a
size limit. macOS still asks the owner once per app ("JARVIS wants to control Music").
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_SCRIPT = 4000  # characters
RUN_TIMEOUT_S = 30
COMPILE_TIMEOUT_S = 15
MAX_OUTPUT = 400  # characters of a script's result that are spoken

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "script": {"type": "string"},
        "summary": {"type": "string"},
        "reads_only": {"type": "boolean"},
        "possible": {"type": "boolean"},
    },
    "required": ["script", "summary", "reads_only", "possible"],
}

SYSTEM = """You write AppleScript for a private assistant on the user's Mac (macOS 15). Return JSON:
- "script": ONE complete AppleScript that does the task, using apps' own AppleScript dictionaries
  (Reminders, Calendar, Notes, Music, Messages, Mail, Safari, Finder, System Events, Contacts, Photos, Shortcuts…).
  If it produces an answer, the script must END with `return <text>` so the answer can be read out.
- "summary": what the script does, one short sentence starting with a verb ("Adds 'Buy milk' to Reminders").
- "reads_only": true only if the script just reads information and changes nothing.
- "possible": false if the task can't be done safely with AppleScript (then script is "").

Never use: do shell script, do script, run script, load script, store script, osascript, «event» codes,
`use framework`, `current application`, do JavaScript, open for access / write (files), delete or move files,
empty the trash, passwords or the keychain, administrator privileges. If the task needs any of these,
set possible to false.
Keep it short. No comments. Use the names the user said; don't invent data you weren't given."""

FEWSHOT: list[tuple[str, dict[str, Any]]] = [
    ("Add a reminder 'Buy milk' in the Reminders app",
     {"script": 'tell application "Reminders"\n\tmake new reminder with properties {name:"Buy milk"}\nend tell',
      "summary": "Adds 'Buy milk' to Reminders", "reads_only": False, "possible": True}),
    ("What song is playing in Music?",
     {"script": 'tell application "Music"\n\tif player state is playing then\n\t\treturn (name of current track) & " by " & '
                '(artist of current track)\n\telse\n\t\treturn "Nothing is playing"\n\tend if\nend tell',
      "summary": "Reads the song playing in Music", "reads_only": True, "possible": True}),
    ("Delete all files in my Downloads folder",
     {"script": "", "summary": "Deleting files isn't allowed", "reads_only": False, "possible": False}),
]

# -- classification (code, not the model) -----------------------------------------------------
# one strike and the script never runs
_BLOCKED: list[tuple[str, str]] = [
    (r"\bdo\s+shell\s+script\b", "runs a shell command"),
    (r"\bdo\s+script\b", "runs a command in Terminal"),
    (r"\b(?:run|load|store)\s+script\b", "runs another script"),
    (r"\bosascript\b|\bosacompile\b", "runs another script"),
    (r"[«»]|\bevent\s+[a-z]{8}\b", "uses raw Apple event codes"),
    (r"\buse\s+framework\b|\bcurrent\s+application\b|\bNS[A-Z]\w+", "uses the Objective-C bridge"),
    (r"\bdo\s+JavaScript\b|\bexecute\b[^\n]*\bjavascript\b", "runs JavaScript in a web page"),
    (r"\bopen\s+for\s+access\b|\bwrite\b[^\n]*\bto\b|\bset\s+eof\b", "writes files"),
    (r"\bempty\b[^\n]*\btrash\b|\btrash\b[^\n]*\bempty\b", "empties the Trash"),
    (r"\bpassword\b|\bkeychain\b|\bcredential", "touches passwords"),
    (r"\badministrator\s+privileges\b|\bsudo\b|\bwith\s+prompt\b", "asks for admin rights"),
    (r'\btell\s+application\s+"(?:Terminal|iTerm2?|Script Editor|Automator|Shortcuts Events|Keychain Access|Console)"',
     "controls a tool that can run code"),
    (r"\bdo\s+shortcut\b|\brun\s+shortcut\b", "runs a Shortcut, which can run code"),
    (r'\b(?:tell|of|to)\s+application\s+(?!")|\bapplication\s+id\b|\bapplication\s+file\b', "names an app indirectly"),
    (r"\bwrite\s+text\b", "types into a terminal"),
]
# Finder / System Events deleting or moving files: never (files.trash exists, with confirmation)
_FILE_TARGET = r'tell\s+application\s+"(?:Finder|System Events)"'
_FILE_DESTRUCTIVE = r"\b(?:delete|move|duplicate)\b[^\n]*(?:\bfile\b|\bfolder\b|\bitems?\b|\bPOSIX\b|\balias\b|\bdisk\b|\bentire contents\b)"

# anything that isn't clearly reading counts as a change
_CHANGES = re.compile(
    r"\b(?:make|delete|remove|move|duplicate|open|close|quit|activate|launch|reopen|save|send|reply|forward|click|keystroke|"
    r"key\s+code|key\s+down|key\s+up|play|pause|playpause|stop|next\s+track|previous\s+track|back\s+track|resume|reveal|"
    r"select|print|display|say|beep|restart|shut\s+down|sleep|log\s+out|mount|eject|import|export|add|create|"
    r"show|hide|perform|start|cancel|mark|flag|archive|empty|set\s+the\s+clipboard|set\s+volume|open\s+location|"
    r"search|refresh|reload|run|launch)\b",
    re.I,
)
_SET = re.compile(r"\bset\s+(.+?)\s+to\b", re.I)
_COPY = re.compile(r"\bcopy\s+.+?\s+to\s+(.+)$", re.I | re.M)


@dataclass
class Script:
    task: str
    script: str
    summary: str
    reads_only: bool  # the model's claim
    verdict: str = ""  # blocked | read | change (the classifier's)
    reason: str = ""  # why it's blocked


def _strip_strings(script: str) -> str:
    """Quoted text removed: a reminder called "delete the tab" is data, not a command."""
    return re.sub(r'"(?:[^"\\]|\\.)*"', '""', script)


def _local_name(target: str) -> bool:
    """`set x to …` / `copy … to x` only store a result in a variable when the target is a bare name."""
    t = target.strip()
    return re.fullmatch(r"(?:my\s+)?[A-Za-z_]\w*|\{[A-Za-z_\w,\s]*\}", t) is not None and t.lower() not in (
        "volume", "clipboard", "the clipboard")


def classify(script: str) -> tuple[str, str]:
    """-> ("blocked", reason) | ("read", "") | ("change", "")."""
    if not script.strip():
        return "blocked", "the script is empty"
    if len(script) > MAX_SCRIPT:
        return "blocked", "the script is too long"
    code = _strip_strings(script)
    for pat, why in _BLOCKED:
        # app names and secrets live in quotes; commands in the code ("write to Mom" in a message is fine)
        where = script if why in ("controls a tool that can run code", "touches passwords", "names an app indirectly") else code
        if re.search(pat, where, re.I):
            return "blocked", why
    if re.search(_FILE_TARGET, script, re.I) and re.search(_FILE_DESTRUCTIVE, code, re.I):
        return "blocked", "deletes or moves files"
    lines = [ln.strip() for ln in code.splitlines()]
    for ln in lines:
        body = re.sub(r"^\s*tell\s+application\s+\"\"\s*(?:to\s+)?", "", ln, flags=re.I)
        if _CHANGES.search(re.sub(r"^\s*(?:end|on|if|else|repeat|try|on error|return|exit)\b", "", body, flags=re.I)):
            return "change", ""
        for m in _SET.finditer(body):
            if not _local_name(m.group(1)):
                return "change", ""
        for m in _COPY.finditer(body):
            if not _local_name(m.group(1)):
                return "change", ""
    return "read", ""


# -- running (macOS) -------------------------------------------------------------------------
class ScriptError(RuntimeError):
    pass


def _error_text(err: str) -> str:
    if "-1743" in err or "Not authorized" in err or "not allowed" in err.lower():
        return "not allowed to control that app: enable it in System Settings → Privacy & Security → Automation"
    return err.strip().splitlines()[-1][:200] if err.strip() else "the script failed"


def compile_check(script: str) -> str | None:
    """None when it compiles, else the compiler's message. Compiling runs nothing."""
    with tempfile.TemporaryDirectory() as d:
        src, out = Path(d) / "task.applescript", Path(d) / "task.scpt"
        src.write_text(script, encoding="utf-8")
        try:
            r = subprocess.run(["osacompile", "-o", str(out), str(src)], capture_output=True, text=True,
                               timeout=COMPILE_TIMEOUT_S)
        except FileNotFoundError:
            return None  # not on a Mac: nothing to check with (tests inject their own)
        except subprocess.TimeoutExpired:
            return "the compiler didn't answer"
        return None if r.returncode == 0 else (r.stderr.strip().splitlines() or ["syntax error"])[-1][:300]


def run_script(script: str) -> str:
    """Run the (already classified and, where needed, confirmed) script; its result as text."""
    try:
        r = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
    except FileNotFoundError as exc:
        raise ScriptError("AppleScript is only available on a Mac") from exc
    except subprocess.TimeoutExpired as exc:
        raise ScriptError("the app didn't respond in time") from exc
    if r.returncode != 0:
        raise ScriptError(_error_text(r.stderr))
    return r.stdout.strip()


# -- writing ---------------------------------------------------------------------------------
class ScriptAgent:
    """Writes, checks and runs AppleScripts for ``mac.do``."""

    def __init__(
        self,
        chat: Callable[[list[dict[str, str]], dict[str, Any]], dict[str, Any]],
        compiler: Callable[[str], str | None] = compile_check,
        runner: Callable[[str], str] = run_script,
    ):
        self.chat = chat  # (messages, schema) -> JSON, the brain's current model
        self.compiler = compiler
        self.runner = runner

    @staticmethod
    def messages(task: str, error: tuple[str, str] | None = None) -> list[dict[str, str]]:
        import json

        msgs = [{"role": "system", "content": SYSTEM}]
        for t, ex in FEWSHOT:
            msgs.append({"role": "user", "content": f"Task: {t}"})
            msgs.append({"role": "assistant", "content": json.dumps(ex)})
        msgs.append({"role": "user", "content": f"Task: {task}"})
        if error is not None:
            bad, why = error
            msgs.append({"role": "assistant", "content": json.dumps({"script": bad})})
            msgs.append({"role": "user", "content": f"That script doesn't compile: {why}. Return a corrected one."})
        return msgs

    def write(self, task: str) -> Script:
        """The model's script for ``task``, compiled and classified. Raises ScriptError when
        no usable script comes back."""
        task = " ".join(task.split())[:400]
        if not task:
            raise ScriptError("I didn't catch what to do")
        error: tuple[str, str] | None = None
        for _ in range(2):  # one retry after a compile error
            try:
                data = self.chat(self.messages(task, error), SCHEMA)
            except ValueError as exc:
                raise ScriptError("I couldn't write a script for that") from exc
            if data.get("possible") is False:
                raise ScriptError(str(data.get("summary") or "that can't be done safely with AppleScript").rstrip("."))
            script = str(data.get("script") or "").replace("\r\n", "\n").strip()
            summary = " ".join(str(data.get("summary") or "").split())[:160] or task
            s = Script(task, script, summary, data.get("reads_only") is True)
            s.verdict, s.reason = classify(script)
            if s.verdict == "blocked":
                return s  # never compiled or run; the caller explains why
            problem = self.compiler(script)
            if problem is None:
                if s.verdict == "read" and not s.reads_only:
                    s.verdict = "change"  # both must agree it only reads
                return s
            log.info("generated script didn't compile: %s", problem)
            error = (script, problem)
        raise ScriptError("I couldn't write a working script for that")

    def run(self, s: Script) -> str:
        if s.verdict not in ("read", "change"):
            raise ScriptError("that script isn't allowed to run")
        if classify(s.script)[0] == "blocked":  # the script is re-checked right before it runs
            raise ScriptError("that script isn't allowed to run")
        out = self.runner(s.script)
        return out if len(out) <= MAX_OUTPUT else out[: MAX_OUTPUT - 1] + "…"
