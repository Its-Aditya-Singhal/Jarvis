"""Stand-ins for the camera, microphone and every model, driven by a scripted scene.

They let the whole service (API, face/voice/liveness pipelines, speech,
brain, tools, memory) run end to end on a machine without a camera,
microphone, Apple GPU, downloaded models or Ollama: the test suite on Linux,
and ``python -m jarvis.headless`` for working on the UI.

A :class:`Scene` describes what is in front of the "Mac": who is at the
screen, whether they cooperate with the prompts on screen (enrollment poses,
liveness challenges), and what is said out loud. The fakes turn it into the
same observations the real models produce, so the real authentication logic
(template matching, liveness gate, fusion, levels) is what gets tested.

Nothing here is used by the normal app.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .auth.face.types import FaceObservation
from .speech.stt import Transcript
from .speech.text import has_devanagari

FRAME_W, FRAME_H = 640, 480
BLOCK = 512  # the microphone block size (32 ms at 16 kHz)
SR = 16000
_SPEECH_BASE = 0.25  # speech blocks carry an utterance id in their amplitude
_ID_SCALE = 4096.0
_MAX_ID = 2900  # keeps the amplitude below the clipping check (0.98)


def _unit(seed: str, dim: int) -> np.ndarray:
    rng = np.random.default_rng(int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "little"))
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _jitter(base: np.ndarray, amount: float, rng: np.random.Generator) -> np.ndarray:
    v = base + amount * rng.standard_normal(base.shape).astype(np.float32) / np.sqrt(base.size)
    return (v / np.linalg.norm(v)).astype(np.float32)


# identities: same person = nearly the same vector, different people = unrelated vectors
FACES = {name: _unit(f"face:{name}", 512) for name in ("owner", "stranger", "bystander")}
VOICES = {name: _unit(f"voice:{name}", 192) for name in ("owner", "stranger")}


@dataclass
class Spoken:
    text: str
    speaker: str  # owner | stranger
    lang: str
    duration_s: float


@dataclass
class Scene:
    """What the fake camera sees and the fake microphone hears."""

    person: str = "owner"  # owner | stranger | photo (the owner's photo) | nobody
    bystanders: int = 0  # extra unknown faces in view
    cooperative: bool = True  # follows the prompts on screen (poses, liveness challenges)
    blinking: bool = True  # natural blinking (a photo never blinks)
    frozen: bool = False  # the camera repeats one frame (virtual camera / paused video)
    # the instruction currently on screen: an enrollment step or a liveness challenge step
    instruction: Callable[[], str | None] = lambda: None
    # the microphone only "speaks" while the assistant isn't talking (people take turns)
    can_speak: Callable[[], bool] = lambda: True
    said: list[str] = field(default_factory=list)  # everything the assistant said aloud (whole replies)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: list[Spoken] = []
        self._utterances: dict[int, Spoken] = {}
        self._next_id = 1
        self._frame_n = 0
        self._step: str | None = None
        self._step_frames = 0
        self._rng = np.random.default_rng(7)

    # -- speech ----------------------------------------------------------------
    def say(self, text: str, speaker: str = "owner", lang: str | None = None, duration_s: float | None = None) -> None:
        """Someone says ``text`` out loud (queued; the microphone plays it in order)."""
        lang = lang or ("hi" if has_devanagari(text) else "en")
        words = len(re.findall(r"\w+", text))
        dur = duration_s if duration_s is not None else min(8.0, max(0.6, 0.4 * words))
        with self._lock:
            self._queue.append(Spoken(text, speaker, lang, dur))

    @property
    def speech_pending(self) -> bool:
        with self._lock:
            return bool(self._queue)

    def _take_utterance(self) -> tuple[int, Spoken] | None:
        with self._lock:
            if not self._queue:
                return None
            sp = self._queue.pop(0)
            uid = self._next_id
            self._next_id = self._next_id % _MAX_ID + 1
            self._utterances[uid] = sp
            return uid, sp

    def heard(self, audio: np.ndarray) -> Spoken | None:
        """The utterance a stretch of fake microphone audio carries (None for silence)."""
        a = np.abs(np.asarray(audio, dtype=np.float32))
        loud = a[a > 0.1]
        if not loud.size:
            return None
        ids, counts = np.unique(np.rint((loud - _SPEECH_BASE) * _ID_SCALE).astype(int), return_counts=True)
        uid = int(ids[np.argmax(counts)])
        with self._lock:
            return self._utterances.get(uid)

    # -- vision ------------------------------------------------------------------
    def _pose(self) -> dict:
        """Head pose and expression for this frame, following the on-screen instruction."""
        step = self.instruction() if self.cooperative else None
        if step != self._step:
            self._step, self._step_frames = step, 0
        self._step_frames += 1
        ramp = min(self._step_frames, 6) / 6  # people move over a few frames, not instantly
        pose = {"yaw": 0.0, "pitch": 0.0, "width": 0.30, "smile": 1.0}
        if step in ("left", "turn_left"):
            pose["yaw"] = 32.0 * ramp
        elif step in ("right", "turn_right"):
            pose["yaw"] = -32.0 * ramp
        elif step == "up":
            pose["pitch"] = 14.0 * ramp
        elif step == "down":
            pose["pitch"] = -14.0 * ramp
        elif step == "closer":
            pose["width"] = 0.30 * (1 + 0.45 * ramp)
        elif step == "farther":
            pose["width"] = 0.30 * (1 - 0.3 * ramp)
        elif step == "smile":
            pose["smile"] = 1.25
        return pose

    def observe(self) -> list[FaceObservation]:
        """The faces the face engine would report for the current frame."""
        with self._lock:
            self._frame_n += 1
            n = self._frame_n
        faces: list[FaceObservation] = []
        if self.person != "nobody":
            identity = "owner" if self.person in ("owner", "photo") else self.person
            live = self.person != "photo"
            pose = self._pose()
            # blink every 6th frame (a blink spans one analysed frame)
            eyes = 0.1 if (live and self.blinking and n % 6 == 0) else 0.3
            faces.append(self._face(identity, pose, eyes, 0.95 if live else 0.05, cx=0.5))
        for i in range(self.bystanders):
            faces.append(self._face("bystander", {"yaw": 0.0, "pitch": 0.0, "width": 0.18, "smile": 1.0},
                                    0.3, 0.95, cx=0.15 + 0.7 * (i % 2)))
        return faces

    def _face(self, identity: str, pose: dict, eye_open: float, live_score: float, cx: float) -> FaceObservation:
        w = pose["width"] * FRAME_W
        x1, y1 = cx * FRAME_W - w / 2, FRAME_H / 2 - w * 0.6
        bbox = np.array([x1, y1, x1 + w, y1 + w * 1.2], dtype=np.float32)
        lm = np.zeros((68, 2), dtype=np.float32)
        lm[36], lm[45] = (x1 + 0.2 * w, y1 + 0.4 * w), (x1 + 0.8 * w, y1 + 0.4 * w)  # outer eye corners
        half_mouth = 0.2 * w * pose["smile"]
        lm[48], lm[54] = (x1 + 0.5 * w - half_mouth, y1 + 0.9 * w), (x1 + 0.5 * w + half_mouth, y1 + 0.9 * w)
        return FaceObservation(
            bbox=bbox, det_score=0.9, embedding=_jitter(FACES[identity], 0.25, self._rng),
            pitch=pose["pitch"], yaw=pose["yaw"], roll=0.0, landmarks=lm, quality=0.9,
            frame_width=FRAME_W, frame_height=FRAME_H, eye_open=eye_open, live_score=live_score,
        )

    def frame(self) -> np.ndarray:
        img = np.full((FRAME_H, FRAME_W, 3), 90, dtype=np.uint8)
        if not self.frozen:  # a real sensor never repeats a frame bit for bit
            with self._lock:
                n = self._frame_n + int(self._rng.integers(1 << 30))
            img[0, :8, 0] = np.frombuffer(n.to_bytes(8, "little"), dtype=np.uint8)
        return img


# -- camera & face ------------------------------------------------------------------------
class FakeCamera:
    def __init__(self, scene: Scene):
        self.scene = scene
        self.status, self.error = "off", None

    def start(self) -> None:
        self.status = "active"

    def stop(self) -> None:
        self.status = "off"

    def latest(self, max_age_s: float = 1.0) -> np.ndarray | None:
        return self.scene.frame() if self.status == "active" else None


class _Passive:
    ready, error = True, None


class FakeFaceEngine:
    """Reports the scene's faces, with the fields the real InsightFace pipeline fills."""

    def __init__(self, scene: Scene):
        self.scene = scene
        self.ready, self.error = False, None
        self.liveness = _Passive()

    def load(self) -> bool:
        self.ready = True
        return True

    def analyze(self, frame: np.ndarray) -> list[FaceObservation]:
        return self.scene.observe()


# -- microphone & voice ---------------------------------------------------------------
class FakeMicrophone:
    """Plays the scene's queued utterances as 16 kHz blocks, then silence."""

    def __init__(self, scene: Scene, realtime: bool = False):
        self.scene = scene
        self.realtime = realtime
        self.status, self.error, self.device_name, self.level = "off", None, "Fake microphone", 0.0
        self._blocks: list[np.ndarray] = []
        self._current: tuple[int, Spoken] | None = None
        self._interrupted = False
        self._lock = threading.Lock()

    def start(self) -> None:
        self.status = "active"

    def stop(self) -> None:
        self.status, self.level = "off", 0.0

    def drain(self) -> None:
        with self._lock:
            self._blocks.clear()
            self._current = None

    def _render(self, uid: int, sp: Spoken) -> list[np.ndarray]:
        quiet = np.full(BLOCK, 1e-3, dtype=np.float32)
        loud = np.full(BLOCK, _SPEECH_BASE + uid / _ID_SCALE, dtype=np.float32)
        loud[::2] *= -1  # zero mean, like sound
        n = max(1, int(sp.duration_s * SR / BLOCK))
        # the voice pipeline closes an utterance after 0.45 s of silence (15 blocks): once the
        # last block has been read, the utterance has been heard
        return [quiet] * 8 + [loud] * n + [quiet] * 16

    def read(self, timeout: float = 0.5) -> np.ndarray | None:
        if self.status != "active":
            time.sleep(min(timeout, 0.05))
            return None
        with self._lock:
            if not self.scene.can_speak():
                # the assistant is talking and drops what it hears: like a person, wait
                # for it to finish, then say the interrupted sentence again
                self._interrupted = self._interrupted or len(self._blocks) > 1
                block = None
            else:
                if self._interrupted and self._current is not None:
                    self._blocks, self._interrupted = self._render(*self._current), False
                if not self._blocks and self.scene.speech_pending:
                    self._current = self.scene._take_utterance()
                    if self._current is not None:
                        self._blocks = self._render(*self._current)
                block = self._blocks.pop(0) if self._blocks else None
        if block is None:
            time.sleep(0.02)
            self.level = 0.0
            return None
        if self.realtime:
            time.sleep(BLOCK / SR)
        self.level = float(min(1.0, np.abs(block).mean() * 3))
        return block


def fake_vad() -> Callable[[np.ndarray], float]:
    return lambda frame: 1.0 if float(np.abs(frame).max()) > 0.1 else 0.0


class FakeSpeakerEngine:
    """Speaker embeddings: the owner's voice matches the owner, anyone else doesn't."""

    def __init__(self, scene: Scene):
        self.scene = scene
        self.ready, self.error = False, None
        self._rng = np.random.default_rng(11)

    def load(self) -> bool:
        self.ready = True
        return True

    def embed(self, audio: np.ndarray) -> np.ndarray:
        sp = self.scene.heard(audio)
        who = sp.speaker if sp is not None and sp.speaker in VOICES else "stranger"
        return _jitter(VOICES[who], 0.2, self._rng)

    def embed_windows(self, audio: np.ndarray, win_s: float = 2.0, hop_s: float = 1.0) -> list[np.ndarray]:
        n = 1 + max(0, int((len(audio) / SR - win_s) // hop_s) + 1)
        return [self.embed(audio) for _ in range(min(n, 6))]


# -- speech ---------------------------------------------------------------------------
class FakeSTT:
    """Transcribes fake microphone audio back to the text the scene said."""

    size, engine = "fake", "fake"

    def __init__(self, scene: Scene):
        self.scene = scene
        self.ready, self.error = False, None
        self.calls = 0

    def load(self) -> bool:
        self.ready = True
        return True

    def transcribe(self, audio: np.ndarray, prompt: str | None = None, language: str | None = None) -> Transcript:
        self.calls += 1
        sp = self.scene.heard(audio)
        if sp is None:
            return Transcript("", language or "en", 1.0, len(audio) / SR)
        return Transcript(sp.text, sp.lang, 1.0, len(audio) / SR)


class FakeTTS:
    """Voice synthesis producing a few milliseconds of silence."""

    def __init__(self, scene: Scene, speed: float = 1.0):
        self.scene = scene
        self.speed = speed
        self.ready, self.error = False, None

    def load(self) -> bool:
        self.ready = True
        return True

    def synth(self, text: str, gender: str) -> np.ndarray:
        return np.zeros(240, dtype=np.float32)  # 10 ms at 24 kHz


class FakePlayer:
    def play(self, audio, sr, on_level, stop) -> None:
        on_level(0.0)


# -- language model ---------------------------------------------------------------------
def bag_of_words(text: str, dim: int = 256) -> list[float]:
    """A deterministic stand-in for sentence embeddings: shared words = similar vectors."""
    v = np.zeros(dim, dtype=np.float32)
    for w in re.findall(r"\w+", text.lower()):
        v[int.from_bytes(hashlib.md5(w.encode()).digest()[:4], "little") % dim] += 1.0
    n = float(np.linalg.norm(v))
    return (v / n if n else v).tolist()


class FakeOllama:
    """OllamaClient look-alike. ``script`` maps a phrase to the JSON the model returns
    for any request containing it (case-insensitive); ``responder(user_text, schema)``
    handles the rest (by default a canned short answer)."""

    def __init__(self, models: list[str] | None = None, responder: Callable[[str, dict], dict] | None = None,
                 up: bool = True, script: dict[str, dict] | None = None):
        self.installed = models if models is not None else ["qwen2.5:7b", "qwen2.5:3b", "bge-m3:latest"]
        self.responder = responder or self.default_reply
        self.script = {k.lower(): v for k, v in (script or {}).items()}
        self.up = up
        self.requests: list[dict] = []
        self._loaded: dict[str, int] = {}

    def _check(self) -> None:
        if not self.up:
            from .llm.client import LLMUnavailable

            raise LLMUnavailable("Ollama not reachable: fake server is down")

    @staticmethod
    def default_reply(text: str, schema: dict) -> dict:
        if "facts" in schema.get("properties", {}):
            return {"facts": []}
        return {"actions": [], "reply": "This is a test reply from the fake language model."}

    def models(self) -> list[str]:
        self._check()
        return sorted(self.installed)

    def chat_json(self, model, messages, schema, temperature=0.2, keep_alive="20m", num_predict=None) -> dict:
        self._check()
        self._loaded[model] = 4 << 30
        user = messages[-1]["content"] if messages else ""
        text = user.split("]", 1)[1].strip() if user.startswith("[Now:") else user
        text = text.split("\n")[-1].strip()  # after any "[Remembered: …]" line
        self.requests.append({"model": model, "text": text, "messages": messages})
        if "actions" in schema.get("properties", {}):
            low = text.lower()
            for phrase, out in self.script.items():
                if phrase in low:
                    return out
        return self.responder(text, schema)

    def embed(self, model: str, texts: list[str], keep_alive: str = "10m") -> list[list[float]]:
        self._check()
        return [bag_of_words(t) for t in texts]

    def warm(self, model: str) -> None:
        self._check()
        self._loaded[model] = 4 << 30

    def loaded(self) -> dict[str, int]:
        return dict(self._loaded) if self.up else {}

    def unload(self, model: str) -> None:
        self._loaded.pop(model, None)


class FakeOllamaServer:
    error: str | None = None

    def ensure(self) -> bool:
        return True

    def stop(self) -> None: ...


# -- Mac integrations ---------------------------------------------------------------------
class FakeApp:
    def __init__(self, name: str):
        self.name, self.quit = name, False

    def localizedName(self) -> str:
        return self.name

    def activationPolicy(self) -> int:
        return 0

    def terminate(self) -> bool:
        self.quit = True
        return True


class FakeMac:
    """Records the commands MacControl would run (``open``, ``osascript``, ``pmset``)."""

    def __init__(self, running: list[str] | None = None):
        self.calls: list[list[str]] = []
        self.apps = [FakeApp(n) for n in (running or ["Finder", "Safari", "Spotify"])]
        self.volume, self.muted = 40, False

    def run(self, argv: list[str]) -> str:
        self.calls.append(list(argv))
        if argv[:3] == ["osascript", "-e", "get volume settings"]:
            return f"output volume:{self.volume}, input volume:50, alert volume:100, output muted:{str(self.muted).lower()}"
        if argv[:2] == ["pmset", "-g"]:
            return "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t80%; charged"
        return ""

    def running(self) -> list[FakeApp]:
        return [a for a in self.apps if not a.quit]


def fake_osascript(script: str, *args: str) -> str:
    """AppleBridge runner for machines without Calendar/Notes: no calendars, no notes."""
    return ""


def fake_apps_dir(root: Path, names: tuple[str, ...] = ("Safari", "Notes", "Calendar", "Spotify", "Visual Studio Code")) -> Path:
    """A folder of empty ``.app`` bundles for AppIndex to find."""
    d = root / "Applications"
    for n in names:
        (d / f"{n}.app").mkdir(parents=True, exist_ok=True)
    return d
