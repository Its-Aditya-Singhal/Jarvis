"""Speech I/O: transcribes utterances, detects the wake word, answers aloud.

Utterances come from the voice pipeline (already cut by VAD and judged by
speaker verification). Each is transcribed locally. Anything not addressed to
the assistant by name is discarded immediately — it is never shown, logged or
stored. Addressed utterances are acted on only when the owner is verified
(face + liveness); a clearly different voice is refused even then.

Until the local LLM arrives (phase 5) the assistant can hear and answer but
not carry out commands, and says so rather than pretending.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from typing import Callable

import numpy as np

from .config import Settings
from .database.db import Database
from .events import EventBus
from .speech.output import SpeechOutput
from .speech.stt import SpeechToText, Transcript
from .speech.text import phrase_match
from .speech.tts import GENDERS, TextToSpeech
from .speech.wake import find_wake

log = logging.getLogger(__name__)

PHRASE_MATCH_MIN = 0.55  # enrollment: spoken words vs displayed phrase
UNAUTHORIZED_EVENT_GAP_S = 20.0
STOP_WORDS = {"stop", "dismiss", "enough", "okay", "ok", "bas", "band", "ruko", "chup", "बस", "बंद", "रुको", "चुप"}
YES_WORDS = {"yes", "yeah", "yep", "confirm", "confirmed", "sure", "haan", "han", "haa", "ha", "ji", "हाँ", "हां", "हा", "जी"}
YES_PHRASES = ("go ahead", "do it", "delete it", "kar do", "kardo", "कर दो", "कर दीजिए", "हटा दो")
NO_WORDS = {"no", "nope", "cancel", "don't", "dont", "nahi", "nahin", "mat", "नहीं", "नही", "मत", "रहने"}


def confirm_answer(text: str) -> bool | None:
    """True for yes, False for no, None if the utterance is neither."""
    low = text.lower()
    words = set(re.findall(r"[\w\u0900-\u097F']+", low))
    if words & NO_WORDS:
        return False
    if words & YES_WORDS or any(p in low for p in YES_PHRASES):
        return True
    return None


def name_prompt(assistant: str, owner: str = "") -> str:
    """Whisper prompt that teaches the spelling of the names.

    It must describe the names rather than end with them: Whisper treats the
    prompt as preceding speech, and a prompt ending in "Friday." makes it skip
    a spoken "Friday" as already transcribed. ALL-CAPS names are title-cased
    so they aren't read as acronyms.
    """
    fix = lambda n: n.title() if n.isupper() and len(n) > 3 else n
    text = f"The assistant is called {fix(assistant)}."
    return text + (f" The user is {fix(owner)}." if owner else "")


def common_phrases(owner: str) -> list[str]:
    """Replies worth synthesising at startup so they play instantly."""
    return [
        f"Authentication approved. Hi {owner}, how may I help you today?",
        "Quick liveness check. Follow the prompts.",
        "Authentication required. I only take commands from my verified owner.",
        "That voice doesn't match my owner. Command blocked.",
        "Authentication failed. You are not my boss.",
        "Sorry, I didn't catch that.",
        "To do that I need to hear your voice. Please say it out loud.",
        "I couldn't confirm your voice. Please say that again.",
        "Okay, I won't delete it.",
        "Note saved.",
    ]


def placeholder_reply(command: str, lang: str, gender: str) -> str:
    """Answer used when no assistant brain is attached (e.g. LLM disabled)."""
    short = command if len(command) <= 80 else command[:77] + "…"
    if lang == "hi":
        verb = "पाऊँगा" if gender == "male" else "पाऊँगी"
        return f"मैंने सुना: {short}. लोकल लैंग्वेज मॉडल जुड़ने के बाद मैं यह काम कर {verb}."
    return f"I heard: {short}. I'll be able to act on commands once my local language model is connected."


class SpeechService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        bus: EventBus,
        stt: SpeechToText,
        tts: TextToSpeech,
        owner_verified: Callable[[], bool],
        names: Callable[[], tuple[str, str]],
        player=None,
        on_command: Callable[[str, str], None] | None = None,
    ):
        self.s = settings
        self.db = db
        self.bus = bus
        self.stt = stt
        self.tts = tts
        self.owner_verified = owner_verified
        self.names = names
        self.on_command = on_command  # (text, language) -> handled by the assistant brain
        # set by the assistant service when tools are enabled
        self.alarm_ringing: Callable[[], bool] = lambda: False
        self.dismiss_alarm: Callable[[], int] = lambda: 0
        # set by the assistant service: spoken yes/no for a pending deletion
        self.confirm_pending: Callable[[], bool] = lambda: False
        self.on_confirm: Callable[[bool, str | None], None] = lambda accept, verdict: None
        self.on_voice_mismatch: Callable[[], None] = lambda: None
        self.out = SpeechOutput(tts, bus, self.voice_gender, player=player)
        self._q: queue.Queue[tuple[np.ndarray, str | None]] = queue.Queue(maxsize=3)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listen_until = 0.0
        self._last_unauthorized = 0.0
        bus.on("say", lambda e: self.out.say(e["text"]))

    # -- settings --------------------------------------------------------------
    def voice_gender(self) -> str:
        g = self.db.get("voice_gender", "female")
        return g if g in GENDERS else "female"

    def set_voice_gender(self, gender: str) -> None:
        if gender not in GENDERS:
            raise ValueError("gender must be female or male")
        self.db.set("voice_gender", gender)

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        if self.stt.load():
            where = "Apple GPU" if getattr(self.stt, "engine", "") == "mlx" else "CPU"
            self.bus.log(f"Speech recognition loaded (Whisper {self.stt.size}, {where})")
        else:
            self.bus.log(self.stt.error or "Speech recognition unavailable", "error")
        if self.tts.load():
            self.bus.log("Voice synthesis loaded (Kokoro)")
        else:
            self.bus.log(self.tts.error or "Voice synthesis unavailable", "error")
        self.out.start()
        if self.tts.ready:
            assistant, owner = self.names()
            threading.Thread(target=self.out.prewarm, args=(common_phrases(owner),), name="tts-prewarm", daemon=True).start()
        self._thread = threading.Thread(target=self._loop, name="speech-in", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.out.close()
        try:
            self._q.put_nowait((np.zeros(0, np.float32), None))
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=2)

    def status(self) -> dict:
        return {
            "stt": "ready" if self.stt.ready else (self.stt.error or "loading"),
            "tts": "ready" if self.tts.ready else (self.tts.error or "loading"),
        }

    def muted(self) -> bool:
        return self.out.muted()

    @property
    def listening(self) -> bool:
        return time.monotonic() < self._listen_until

    # -- enrollment support ------------------------------------------------------
    def check_phrase(self, audio: np.ndarray, expected: str, lang_hint: str) -> tuple[bool, str]:
        """Did the speaker actually say the displayed phrase? (True, heard) if
        speech recognition is unavailable, so enrollment still works."""
        if not self.stt.ready:
            return True, ""
        lang = "en" if lang_hint == "English" else "hi" if lang_hint == "Hindi" else None
        assistant, owner = self.names()
        t = self.stt.transcribe(audio, prompt=name_prompt(assistant, owner), language=lang)
        ok = phrase_match(expected, t.text) >= PHRASE_MATCH_MIN
        if not ok and lang is not None:
            # Hinglish or code-switched reading: try the other script/language once
            t = self.stt.transcribe(audio, prompt=name_prompt(assistant, owner))
            ok = phrase_match(expected, t.text) >= PHRASE_MATCH_MIN
        return ok, t.text

    # -- conversation --------------------------------------------------------------
    def submit(self, audio: np.ndarray, voice_verdict: str | None) -> None:
        """Called from the voice loop for each utterance (non-blocking)."""
        if not self.stt.ready:
            return
        try:
            self._q.put_nowait((audio, voice_verdict))
        except queue.Full:
            log.debug("speech queue full; dropping utterance")

    def _loop(self) -> None:
        while not self._stop.is_set():
            audio, verdict = self._q.get()
            if self._stop.is_set() or not len(audio):
                continue
            try:
                self.handle(audio, verdict)
            except Exception:
                log.exception("speech turn failed")

    def handle(self, audio: np.ndarray, verdict: str | None) -> None:
        assistant, owner = self.names()
        t0 = time.monotonic()
        tr: Transcript = self.stt.transcribe(audio, prompt=name_prompt(assistant))
        # debug-level only: transcripts must not reach logs in normal operation
        log.debug("transcript %r (%s, verdict=%s, %.2fs)", tr.text, tr.language, verdict, time.monotonic() - t0)
        if not tr.text:
            return
        if self.alarm_ringing() and STOP_WORDS & set(re.findall(r"[\w\u0900-\u097F]+", tr.text.lower())):
            # like a phone alarm, anyone nearby may silence it
            self.dismiss_alarm()
            return
        found, rest = find_wake(assistant, tr.text)
        followup = time.monotonic() < self._listen_until
        answer = confirm_answer(rest if found else tr.text) if self.confirm_pending() else None
        if not found and not followup and answer is None:
            return  # not addressed to the assistant: discarded, never shown
        command = rest if found else tr.text
        command = command[:1].upper() + command[1:]

        if not self.owner_verified():
            now = time.monotonic()
            if now - self._last_unauthorized > UNAUTHORIZED_EVENT_GAP_S:
                self._last_unauthorized = now
                self.db.add_security_event(
                    "unauthorized_command", "Voice command while the owner was not verified", blocked=True
                )
            self.bus.log("Voice command blocked — owner not verified", "alert")
            self.out.say("Authentication required. I only take commands from my verified owner.")
            return
        if verdict == "rejected":
            self.db.add_security_event(
                "voice_mismatch_command", "Command spoken in a voice that is not the owner's", blocked=True
            )
            self.bus.log("Voice command blocked — voice does not match the owner", "alert")
            self.out.say("That voice doesn't match my owner. Command blocked.")
            self.on_voice_mismatch()
            return
        if answer is not None:
            self.bus.publish({"type": "heard", "text": command, "lang": tr.language})
            self.on_confirm(answer, verdict)
            return

        latency = round(time.monotonic() - t0, 2)
        if not command.strip():
            self.bus.publish({"type": "heard", "text": tr.text, "lang": tr.language, "stt_s": latency})
            self._listen_until = time.monotonic() + self.s.followup_s
            self.bus.publish({"type": "listening", "active": True, "seconds": self.s.followup_s})
            self.out.ping()  # instant "go ahead" instead of a synthesised "Yes?"
            return
        if self._listen_until:
            self._listen_until = 0.0
            self.bus.publish({"type": "listening", "active": False})
        if self.on_command is not None:
            self.on_command(command, tr.language)
            return
        self.bus.publish({"type": "heard", "text": command, "lang": tr.language, "stt_s": latency})
        reply = placeholder_reply(command, tr.language, self.voice_gender())
        self.bus.publish({"type": "reply", "text": reply})
        self.out.say(reply)

    def preview(self, gender: str) -> None:
        assistant, owner = self.names()
        self.out.interrupt()
        self.out.say(f"Hello{' ' + owner if owner else ''}, I'm {assistant or 'your assistant'}. This is how I'll sound.", gender)
