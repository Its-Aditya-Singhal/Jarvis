"""Orchestrates the face + liveness pipeline and composes it with the voice pipeline.

The session counts as the verified owner only when the face matches AND the
liveness gate has confirmed a live person (see ``effective_state``). On top
of that, every request is checked against the auth level the current
evidence allows (``trust``: hard rules + the fusion classifier), and
deletions wait for an explicit confirmation.
"""

from __future__ import annotations

import base64
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import __version__, health, privacy
from .auth.face.continuous import ContinuousFaceAuth, FrameResult
from .auth.face.engine import FaceEngine
from .auth.face.enrollment import EnrollmentSession
from .auth.face.types import FaceObservation
from .auth.fusion.features import FEATURES, Evidence, vector, without_voice
from .auth.fusion.model import FusionModel, load_model
from .auth.fusion.samples import SampleStore
from .auth.fusion.train import train as train_fusion
from .auth.levels import LEVEL_NAMES, REASONS, LevelConfig, Trust, assess
from .auth.liveness.gate import LivenessConfig, LivenessGate, LiveObs
from .auth.liveness.replay import FrozenFeedDetector
from .auth.matching import TemplateMatcher, confidence
from .brain import Brain
from .camera.capture import Camera
from .config import Settings
from .database.db import Database
from .events import EventBus
from .hardware import profile
from .host import responsible_app
from .llm.intents import wait_phrase
from .memory.manager import Memory
from .netguard import NetGuard
from .perf import MODES, PerfMonitor, effective_mode
from .prefs import FACE_PRESETS, LIVENESS_PRESETS, PREFS, VOICE_PRESETS, Prefs
from .security.template_store import TemplateStore
from .speech.stt import SpeechToText
from .speech.text import to_latin
from .speech_service import SpeechService
from .tools.agent import ScriptAgent
from .tools.runner import HARMLESS, LEVELS, Plan, ToolResult, ToolRunner
from .tools.scheduler import AlarmScheduler
from .tools.store import Alarm
from .voice_service import VoiceService

log = logging.getLogger(__name__)


def notify(title: str, text: str) -> None:
    """macOS notification (values passed as argv, never interpolated)."""
    import subprocess

    script = "on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run"
    try:
        subprocess.Popen(["osascript", "-e", script, title, text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass

FACE = "face"
MIN_OWNER_SAMPLES = 10  # before a personal retrain makes sense
PREVIEW_FPS = 12.0  # the on-screen camera view: smooth, independent of the (slower) face analysis
IDLE_FPS = 2.0  # nobody in view for a while: look less often (the owner is picked up within 0.5 s)
NO_FACE_IDLE_S = 10.0
LOW_MEMORY_PCT = profile().low_memory_pct  # system memory use at which idle models are unloaded
IDLE_BEFORE_FREE_S = profile().idle_before_free_s
REENROLL_GRANT_S = 600.0  # after a confirmed face delete/redo, time to scan the new face
RECOVERY_VOICE_S = 60.0  # face missing: a verified voice this recent may start a new scan
MAX_DELAY_S = 6 * 3600  # "after N minutes": longer waits belong in an alarm

# spoken when an action needs a higher level than the evidence allows
BLOCKED_SAY = {
    "voice_needed": ("To do that I need to hear your voice. Please say it out loud.",
                     "यह करने के लिए मुझे आपकी आवाज़ सुननी होगी। कृपया बोलकर कहिए।"),
    "voice_needed_voice": ("I couldn't confirm your voice. Please say that again.",
                           "आपकी आवाज़ पक्की नहीं हो पाई। कृपया दोबारा कहिए।"),
    "voice_needed_typed": ("Typed commands are off in Settings, so please say that out loud.",
                           "सेटिंग्स में टाइप किए कमांड बंद हैं, कृपया बोलकर कहिए।"),
    "voice_rejected": ("An unrecognised voice spoke, so please repeat that yourself.",
                       "कोई अनजान आवाज़ बोली थी, कृपया आप खुद दोबारा कहिए।"),
    "bystander": ("Someone else is in view, so I'll only read things for now.",
                  "कोई और भी दिख रहा है, इसलिए अभी मैं सिर्फ़ जानकारी पढ़ सकती हूँ।"),
    "low_confidence": ("I'm not confident enough that it's you right now.",
                       "अभी मुझे पूरा भरोसा नहीं है कि यह आप ही हैं।"),
    "fresh_liveness": ("First a quick liveness check.", "पहले एक छोटा लाइवनेस चेक।"),
}


@dataclass
class Pending:
    """A deletion (or other level-3 action) waiting for the owner's confirmation."""

    id: str
    plan: Plan
    lang: str
    say: str
    expires: float  # monotonic
    run: Callable[[], ToolResult] | None = None  # set for privacy/settings actions; tools use plan


@dataclass
class Delayed:
    """Actions waiting for a spoken delay ("close it after 10 seconds")."""

    id: str
    due: float  # wall clock (time.time) for the countdown on screen
    summary: str
    timer: threading.Timer


class AssistantService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        store: TemplateStore,
        bus: EventBus,
        engine: FaceEngine,
        camera: Camera,
        voice_factory: Callable[[AssistantService], VoiceService] | None = None,
        speech_factory: Callable[[AssistantService], SpeechService] | None = None,
        brain_factory: Callable[[AssistantService], Brain] | None = None,
        tools_factory: Callable[[AssistantService], tuple[ToolRunner, AlarmScheduler]] | None = None,
        memory_factory: Callable[[AssistantService], Memory] | None = None,
        guard: NetGuard | None = None,
        perf: PerfMonitor | None = None,
    ):
        self.s = settings
        # voice-only (the default): no camera, no face or anti-spoof model, no face loop; the
        # owner is signed in once setup is done and the voice match guards what needs it
        self.face_on = settings.face_auth
        self.clock: Callable[[], float] = time.monotonic  # same clock as the face loop's ``now``
        self.db = db
        self.prefs = Prefs(db)
        self._apply_security_settings()  # before the auth objects below read them
        self.store = store
        self.bus = bus
        self.engine = engine
        self.camera = camera
        self.mode = "idle"  # idle | enrolling | verifying
        self.enrollment: EnrollmentSession | None = None
        self.verifier: TemplateMatcher | None = None
        self.auth = self._new_auth()
        self.live = self._new_gate()
        self.frozen = FrozenFeedDetector()
        self._frozen = False
        self._unlocked = False  # greeted in this presence session
        # "Face once" mode: what the face check at launch established; the camera is off after it
        self._face_session: dict | None = None
        self._boxes: list[list[float]] = []  # face boxes of the last analysed frame, for the preview
        self._stop = threading.Event()
        self._life = threading.Lock()  # start() vs stop(): nothing is switched on once stopping
        self._thread: threading.Thread | None = None
        self._announce: threading.Timer | None = None
        self._last_state_push = 0.0
        self._last_face_t = time.monotonic()
        self._lock = threading.Lock()
        self.brain: Brain | None = brain_factory(self) if brain_factory else None
        self._command_lock = threading.Lock()
        # a delayed action firing while a command runs its tools: one at a time, so two confirmations
        # can't be opened at once (the second silently replaced the first)
        self._tools_lock = threading.Lock()
        # speech first: the voice pipeline hands it utterances and asks it about muting
        self.speech: SpeechService | None = speech_factory(self) if speech_factory else None
        self.voice: VoiceService | None = voice_factory(self) if voice_factory else None
        self.levels = LevelConfig(
            t1=settings.level1_min_prob,
            t2=settings.level2_min_prob,
            t3=settings.level3_min_prob,
            voice_window_s=settings.level2_voice_window_s,
            l3_liveness_max_age_s=settings.level3_liveness_max_age_s,
        )
        self.fusion: FusionModel | None = None
        self.fusion_source = "disabled"
        if settings.fusion_enabled:
            self.fusion, self.fusion_source = load_model(settings.fusion_model_path)
        self.samples = SampleStore(db)
        self._face_quality: float | None = None
        self._pending: Pending | None = None
        self._pending_lock = threading.Lock()
        self._demand_liveness: str | None = None
        self.tools: ToolRunner | None = None
        self.alarms: AlarmScheduler | None = None
        if tools_factory:
            self.tools, self.alarms = tools_factory(self)
            if self.speech is not None:
                self.speech.alarm_ringing = lambda: bool(self.alarms and self.alarms.ringing())
                self.speech.dismiss_alarm = self.dismiss_alarms
        if self.brain is not None and self.tools is not None and (apps := self.tools.apps) is not None:
            self.brain.is_app = lambda name: apps.resolve(name) is not None
        if (brain := self.brain) is not None and self.tools is not None:
            self.tools.agent = ScriptAgent(lambda msgs, schema: brain.chat(
                msgs, schema, temperature=0.1, num_predict=700))
        self._delayed: dict[str, Delayed] = {}
        self._delayed_lock = threading.Lock()
        self.memory: Memory | None = memory_factory(self) if memory_factory else None
        if self.memory is not None:
            if self.tools is not None:
                self.tools.memory = self.memory
            if self.brain is not None:
                self.brain.recall = self.memory.relevant
        if self.speech is not None:
            self.speech.confirm_pending = lambda: self.pending() is not None
            self.speech.on_confirm = self.confirm_spoken
            self.speech.on_voice_mismatch = lambda: self.record_sample(0, "voice_mismatch")
            self.speech.ack = lambda: self.prefs.get("voice.ack")
            self.speech.unverified_reply = self._unverified_reply
            self.speech.everyday = self.everyday
        if (memory := self.memory) is not None and self.brain is not None:
            self.brain.recall = lambda text: memory.relevant(text) if self.prefs.get("memory.enabled") else []
        self.guard = guard or NetGuard()
        self.guard.offline = lambda: self.prefs.get("privacy.offline")
        self.perf = perf or PerfMonitor(self._power_changed, ollama_host=settings.ollama_host)
        self.perf.on_sample = self._check_memory
        self.perf.watched = lambda: self.bus.has_subscribers  # a window is open
        self.perf.ollama = lambda: self.brain is not None and not getattr(self.brain, "gemini", False)
        self._mode = "balanced"
        self._mode_note = ""  # why a mode couldn't fully apply (e.g. a model isn't downloaded)
        self._reenroll: tuple[str, float] | None = None  # (redo | deleted, grant expiry)
        self._told_issues = False
        self.downloader: Any = None  # the first-run model downloader (set by the API)
        self.google: Any = None  # jarvis.google.auth.GoogleAuth (set by the API): Gmail, Drive, Calendar
        self._apply_voice_settings()

    def owner_verified(self) -> bool:
        """Level 1 or higher: the live owner is at the screen."""
        return self.trust().level >= 1

    # -- preferences -------------------------------------------------------------
    def _apply_security_settings(self) -> None:
        p, s = self.prefs, self.s
        s.face_threshold, s.face_reject_threshold = FACE_PRESETS[p.get("security.face")]
        s.voice_threshold, s.voice_reject_threshold = VOICE_PRESETS[p.get("security.voice")]
        s.absence_lock_s = p.get("security.away_lock_s")
        s.liveness_recheck_min_s, s.liveness_recheck_max_s = LIVENESS_PRESETS[p.get("security.liveness")]
        # live objects (absent during __init__)
        if (a := getattr(self, "auth", None)) is not None:
            a.threshold, a.reject_threshold, a.absence_lock_s = s.face_threshold, s.face_reject_threshold, s.absence_lock_s
        if (g := getattr(self, "live", None)) is not None:
            g.cfg.rechallenge_min_s, g.cfg.rechallenge_max_s = s.liveness_recheck_min_s, s.liveness_recheck_max_s
        if (v := getattr(self, "voice", None)) is not None:
            v.auth.threshold, v.auth.reject_threshold = s.voice_threshold, s.voice_reject_threshold

    def _apply_voice_settings(self) -> None:
        self.s.tts_speed = self.prefs.get("voice.speed")
        self.s.followup_s = self.prefs.get("voice.followup_s")
        sp = self.speech
        if sp is not None and getattr(sp.tts, "speed", None) != self.s.tts_speed:
            sp.tts.speed = self.s.tts_speed
            sp.out.clear_cache()  # cached phrases were spoken at the old speed
            if sp.tts.ready:
                from .speech_service import common_phrases

                threading.Thread(target=sp.out.prewarm, args=(common_phrases(self.owner_name),),
                                 name="tts-prewarm", daemon=True).start()

    def set_pref(self, key: str, value) -> dict:
        """Apply a preference. Callers have checked the level (see ``prefs_needs_l3``)."""
        v = self.prefs.set(key, value)
        if key.startswith("security."):
            self._apply_security_settings()
            self.db.add_security_event("settings_changed", f"{key} set to {v}")
            if key == "security.camera" and v == "always" and self.face_on:
                self.end_face_session("Camera set to stay on")
        elif key.startswith("voice."):
            self._apply_voice_settings()
        elif key == "perf.mode":
            self.apply_mode()
        elif key == "privacy.offline":
            self.db.add_security_event("settings_changed", f"offline mode {'on' if v else 'off'}")
        self.bus.log(f"Setting changed: {key} → {v}")
        return self.settings_info()

    def settings_info(self) -> dict:
        return {"prefs": self.prefs.describe(), "mode": self.mode_info(),
                "llm": {"main": self.db.get("llm_model") or self.s.llm_model, "fast": self.fast_model()},
                "provider": getattr(self.brain, "provider", "ollama") if self.brain is not None else "off",
                "face_auth": self.face_on}

    def request_pref(self, key: str, value, lang: str = "en") -> dict:
        """Loosening a security setting: level-3 confirmation first."""
        p = PREFS[key]
        label = p.labels[p.choices.index(self.prefs.get(key))] if p.labels else str(self.prefs.get(key))
        new = p.labels[p.choices.index(value)] if p.labels else str(value)
        what = f"{key.split('.', 1)[1].replace('_s', '').replace('_', ' ')}: {label} → {new}"
        return self.request_sensitive(
            "settings.loosen", what, f"Loosen security ({what})?",
            lambda: ToolResult("settings.loosen", True, f"Done: {what}.", self.set_pref(key, value)),
        )

    # -- performance modes ---------------------------------------------------------
    def fast_model(self) -> str:
        return self.db.get("llm_fast_model") or "qwen2.5:3b"

    def _power_changed(self, on_battery: bool) -> None:
        self.bus.log("On battery power" if on_battery else "On mains power")
        if self.prefs.get("perf.mode") == "auto":
            self.apply_mode()

    def _check_memory(self, stats: dict) -> None:
        """Mac short of memory and the assistant idle: give back the models' memory."""
        b = self.brain
        if b is None or not hasattr(b, "free_memory") or stats.get("system_mem_pct", 0) < LOW_MEMORY_PCT:
            return
        if not stats.get("ollama_mb") or time.monotonic() - b.last_used < IDLE_BEFORE_FREE_S:
            return
        freed = b.free_memory()
        if freed:
            self.bus.log(f"Mac low on memory — unloaded the language model ({freed / 2**30:.1f} GB); "
                         "it reloads on your next question", "warn")

    def apply_mode(self, warm: bool = True) -> None:
        name = effective_mode(self.prefs.get("perf.mode"), self.perf.on_battery)
        mode = MODES[name]
        changed = name != self._mode
        self._mode = name
        self.s.process_fps = min(mode.face_fps, profile().max_face_fps)
        notes = []
        b = self.brain
        if b is not None and hasattr(b, "override") and not getattr(b, "gemini", False):
            want = self.fast_model() if mode.llm == "fast" else None
            if want and want not in (b.installed or b.installed_models()):
                notes.append(f"fast model {want} not installed (ollama pull {want})")
                want = None
            if want != b.override:
                b.override = want
                b.clear()
                b._status = None
                if warm:  # loads the new model and unloads the old one
                    threading.Thread(target=b.start, name="llm-warm", daemon=True).start()
        stt = mode.stt
        if stt == "medium" and not profile().stt_medium:
            stt = "small"
            notes.append("Whisper medium needs more memory than this Mac has; speech stays small")
        if self.speech is not None and isinstance(self.speech.stt, SpeechToText) and self.speech.stt.size != stt:
            threading.Thread(target=self._swap_stt, args=(stt,), name="stt-swap", daemon=True).start()
        elif self.speech is not None and getattr(self.speech.stt, "size", stt) != stt:
            notes.append(f"speech model stays {self.speech.stt.size}")
        self._mode_note = "; ".join(notes)
        if changed:
            self.bus.log(f"Performance mode: {name.capitalize()}" + (f" ({self._mode_note})" if notes else ""))

    def _swap_stt(self, size: str) -> None:
        assert self.speech is not None  # only called when speech is on
        cur = self.speech.stt
        new = SpeechToText(self.s.models_dir, size)
        has_mlx = any((new.mlx_path / f).is_file() for f in ("weights.npz", "weights.safetensors"))
        if not has_mlx and not (new.path / "model.bin").is_file():
            self._mode_note = f"Whisper {size} not downloaded (scripts/download_models.py {size})"
            self.bus.log(f"Speech model {size} not downloaded — keeping {cur.size}", "warn")
            return
        if new.load():
            self.speech.stt = new
            self.bus.log(f"Speech recognition switched to Whisper {size}")
        else:
            self.bus.log(f"Whisper {size} failed to load — keeping {cur.size}", "warn")

    def mode_info(self) -> dict:
        return {"pref": self.prefs.get("perf.mode"), "effective": self._mode, "on_battery": self.perf.on_battery,
                "battery_pct": self.perf.battery_pct, "note": self._mode_note, "stats": self.perf.stats,
                "llm": self.brain.model if self.brain else None,
                "stt": getattr(self.speech.stt, "size", None) if self.speech else None,
                "face_fps": self.s.process_fps, "suggestions": self.suggestions_on()}

    def suggestions_on(self) -> bool:
        return bool(self.prefs.get("memory.enabled") and self.prefs.get("memory.suggestions")
                    and MODES[self._mode].suggestions)

    # -- auth levels -----------------------------------------------------------
    def evidence(self, now: float | None = None) -> Evidence:
        now = self.clock() if now is None else now
        a = self.auth
        ev = Evidence(
            face_state=self.effective_state(),
            liveness=self.live.state if self.s.liveness_enabled and self.face_on else "disabled",
            face_sim=a.smoothed,
            face_age_s=None if a.last_verified_t is None else now - a.last_verified_t,
            face_quality=self._face_quality,
            live_score=self.live.live_score if self.s.liveness_enabled and self.face_on else None,
            live_age_s=None if self.live.passed_t is None else now - self.live.passed_t,
            voice_enrolled=self.voice_enrolled,
        )
        snap = a.snapshot()
        ev.others = max(0, snap.faces - 1)
        ev.bystander = snap.bystander
        if (fs := self._face_session) is not None and self.mode == "verifying":
            # the camera is off: the face and liveness evidence is the launch check's
            ev.face_sim, ev.face_age_s, ev.face_quality = fs["sim"], 0.0, fs["quality"]
            if self.s.liveness_enabled:
                ev.liveness, ev.live_score, ev.live_age_s = "passed", fs["live_score"], 0.0
            ev.others, ev.bystander = 0, False
        v = self.voice
        if v is not None and v.mode == "verifying":
            if v.auth.last is not None:
                ev.voice_sim = v.auth.last.similarity
                ev.voice_age_s = now - v.auth.last.t
            if v.auth.last_verified is not None:
                ev.voice_verified_age_s = now - v.auth.last_verified.t
            ev.voice_rejected_since = v.auth.rejected_since_verified
        return ev

    def _assess(self, now: float | None = None, screen: bool = False) -> tuple[Trust, np.ndarray]:
        ev = self.evidence(now)
        x = vector(ev, self.levels.voice_window_s)
        if screen and self.prefs.get("security.typed") == "on":
            # typed or clicked on the screen of a verified owner: the keyboard stands in for
            # the voice, and the score is the presence score (the one without voice evidence)
            ev.voice_verified_age_s, ev.voice_rejected_since = 0.0, False
            presence = None if self.fusion is None or not self.face_on else float(self.fusion.prob(without_voice(x)))
            return assess(ev, presence, self.levels, presence), x
        if self.fusion is None or not self.face_on:
            # voice-only: the fusion classifier weighs face evidence there is none of; the hard
            # rules decide (signed in after setup, a recent voice match for level 2)
            return assess(ev, None, self.levels), x
        return assess(ev, float(self.fusion.prob(x)), self.levels, float(self.fusion.prob(without_voice(x)))), x

    def trust(self, now: float | None = None, screen: bool = False) -> Trust:
        """``screen``: the request was typed or clicked (see the ``security.typed`` setting)."""
        return self._assess(now, screen)[0]

    def record_sample(self, label: int, source: str) -> None:
        """Keep this moment's feature vector (scores only) for personal retraining."""
        if self.mode != "verifying":
            return
        try:
            self.samples.add(vector(self.evidence(), self.levels.voice_window_s), label, source)
        except Exception:
            log.exception("could not store fusion sample")

    def fusion_info(self) -> dict:
        m = self.fusion
        rep = (m.meta.get("report") or {}).get("test_simulated", {}) if m else {}
        return {
            "enabled": self.s.fusion_enabled,
            "source": self.fusion_source,
            "trained": m.meta.get("trained") if m else None,
            "training_samples": m.meta.get("samples") if m else None,
            "test": {k: rep.get(k) for k in ("n", "accuracy", "auc", "level1", "level2", "level3")} if rep else None,
            "device_samples": self.samples.counts(),
            "min_owner_samples": MIN_OWNER_SAMPLES,
            "features": FEATURES,
        }

    def retrain_fusion(self) -> dict:
        X, y = self.samples.load()
        owner = int((y == 1).sum())
        if owner < MIN_OWNER_SAMPLES:
            raise ValueError(f"need at least {MIN_OWNER_SAMPLES} owner samples from normal use (have {owner})")
        model = train_fusion(X, y)
        model.save(self.s.fusion_model_path)
        self.fusion, self.fusion_source = model, "personal"
        self.db.add_security_event("fusion_retrained", f"Fusion model retrained with {len(y)} device samples")
        self.bus.log(f"Fusion model retrained ({owner} owner / {len(y) - owner} other samples)", "ok")
        return self.fusion_info()

    def reset_fusion(self) -> dict:
        self.s.fusion_model_path.unlink(missing_ok=True)
        self.fusion, self.fusion_source = load_model(None)
        self.db.add_security_event("fusion_reset", "Fusion model reset to the shipped version")
        self.bus.log("Fusion model reset to the shipped version")
        return self.fusion_info()

    def effective_state(self) -> str:
        """Public auth state: face match combined with liveness.

        no_profile | scanning | liveness | approved | denied | absent | spoof
        """
        if self.mode != "verifying":
            return "no_profile"
        if not self.face_on or self._face_session is not None:
            return "approved"
        face = self.auth.state
        if not self.s.liveness_enabled:
            return face
        gate = self.live.state
        if face == "approved":
            return {"passed": "approved", "spoof": "spoof"}.get(gate, "liveness")
        if face == "scanning" and gate == "challenge":
            return "liveness"  # identity dips briefly while turning the head
        return face

    def _new_gate(self) -> LivenessGate:
        s = self.s
        return LivenessGate(
            LivenessConfig(
                challenge_steps=s.liveness_challenge_steps,
                step_timeout_s=s.liveness_step_timeout_s,
                pass_threshold=s.liveness_pass_threshold,
                spoof_threshold=s.liveness_spoof_threshold,
                rechallenge_min_s=s.liveness_recheck_min_s,
                rechallenge_max_s=s.liveness_recheck_max_s,
                blink_gap_s=s.liveness_blink_gap_s,
                lost_reset_s=s.liveness_lost_reset_s,
            )
        )

    def _new_auth(self) -> ContinuousFaceAuth:
        return ContinuousFaceAuth(
            threshold=self.s.face_threshold,
            reject_threshold=self.s.face_reject_threshold,
            window=self.s.face_window,
            reauth_interval_s=self.s.reauth_interval_s,
            absence_lock_s=self.s.absence_lock_s,
        )

    # -- profile -----------------------------------------------------------
    @property
    def owner_name(self) -> str:
        return self.db.get("owner_name", "") or ""

    @property
    def assistant_name(self) -> str:
        return self.db.get("assistant_name", "JARVIS") or "JARVIS"

    @property
    def setup_complete(self) -> bool:
        return self.db.get("setup_complete") == "1"

    @property
    def face_enrolled(self) -> bool:
        return self.store.exists(FACE)

    @property
    def voice_enrolled(self) -> bool:
        return self.voice is not None and self.voice.enrolled

    # -- lifecycle ---------------------------------------------------------
    def _unless_stopping(self, fn: Callable[[], object]) -> bool:
        """Run ``fn`` (switching something on) unless the app is already shutting down.
        Startup loads models for many seconds; the app may be closed meanwhile."""
        with self._life:
            if self._stop.is_set():
                return False
            fn()
            return True

    def start(self) -> None:
        self.bus.log("Core systems online")
        if not self.face_on:
            self._start_voice_only()
            return
        if self.engine.load():
            self.bus.log("Face recognition model loaded")
        else:
            self.bus.log(self.engine.error or "Face model unavailable", "error")

        def begin_seeing() -> None:
            # unlocking needs only the face model and the camera: start verifying now,
            # while speech, voice and the language model (seconds each) load
            self.camera.start()
            if self.setup_complete and self.face_enrolled:
                self.begin_verification()
            elif self.setup_complete and self.voice is not None and self.voice.enrolled:
                self.voice.begin_verification()  # face profile deleted: the voice can unlock a re-scan
            self._thread = threading.Thread(target=self._loop, name="face-loop", daemon=True)
            self._thread.start()
            threading.Thread(target=self._preview_loop, name="preview", daemon=True).start()

        if not self._unless_stopping(begin_seeing):
            return
        if not self._unless_stopping(self.perf.start):
            return
        self.apply_mode(warm=False)  # decide the model first, so only that one is loaded
        brain = self.brain
        if brain is not None:
            # Ollama loads the model in its own process: meanwhile, load speech and voice here
            self._unless_stopping(lambda: threading.Thread(target=self._start_brain, args=(brain,),
                                                           name="llm-start", daemon=True).start())
        to_latin("नमस्ते")  # load the Hindi transliteration tables now (~0.2 s), not on the first Hindi command
        if self.speech is not None:
            self.speech.start()  # loads models, then refuses to start if stopped meanwhile
        if self.voice is not None:
            self.voice.start()
        if self.alarms is not None and not self._unless_stopping(self.alarms.start):
            return
        if self.alarms is not None:
            for a in self.alarms.missed:
                self.bus.log(f"Missed {a.kind} at {a.due:%H:%M} (the app was closed)", "warn")

        def launch() -> None:
            if self.memory is not None:
                # purges expired history and indexes anything saved without embeddings
                threading.Thread(target=self._start_memory, name="memory-start", daemon=True).start()
            self._announce = threading.Timer(12.0, self._announce_issues)
            self._announce.daemon = True
            self._announce.start()

        self._unless_stopping(launch)

    def _start_voice_only(self) -> None:
        """The light start: no camera, face model or face loop. Nothing else loads at launch that
        an everyday command doesn't need (the language model loads on first use)."""
        if self.setup_complete:
            self.begin_verification()
        if not self._unless_stopping(self.perf.start):
            return
        self.apply_mode(warm=False)
        brain = self.brain
        if brain is not None:  # starts Ollama if needed; the model itself loads on demand
            self._unless_stopping(lambda: threading.Thread(target=self._start_brain, args=(brain,),
                                                           name="llm-start", daemon=True).start())
        to_latin("नमस्ते")
        if self.speech is not None:
            self.speech.start()
        if self.voice is not None:
            self.voice.start()
        if self.alarms is not None and not self._unless_stopping(self.alarms.start):
            return
        if self.alarms is not None:
            for a in self.alarms.missed:
                self.bus.log(f"Missed {a.kind} at {a.due:%H:%M} (the app was closed)", "warn")

        def launch() -> None:
            if self.memory is not None:
                threading.Thread(target=self._start_memory, name="memory-start", daemon=True).start()
            self._announce = threading.Timer(12.0, self._announce_issues)
            self._announce.daemon = True
            self._announce.start()

        self._unless_stopping(launch)
        self._push_state()

    def _start_brain(self, brain: Brain) -> None:
        if brain.start():
            if self._stop.is_set():
                brain.stop()  # closed while the model loaded: unload it (and our Ollama) again
                return
            self.bus.log(f"Local language model ready ({brain.model})")
            self.apply_mode()  # Ollama may only now be running: re-check the fast model
        else:
            self.bus.log(f"Language model unavailable: {brain.status()}", "error")

    def _announce_issues(self) -> None:
        """Say once, after startup, if something important is broken."""
        if self._told_issues or self._stop.is_set():
            return
        errors = [i for i in self.issues() if i["level"] == "error"]
        if errors:
            self._told_issues = True
            extra = f" and {len(errors) - 1} more" if len(errors) > 1 else ""
            self.bus.publish({"type": "say", "text": f"Heads up: {errors[0]['title']}{extra}. The fix is on screen."})

    def mic_info(self) -> dict:
        if not self.voice:
            return {"status": "off", "error": "voice pipeline disabled", "device": None}
        m = self.voice.mic
        if hasattr(m, "info"):
            return m.info()
        return {"status": m.status, "error": m.error, "device": m.device_name}

    def issues(self, models: dict | None = None) -> list[dict]:
        st = {"camera": {"status": self.camera.status}, "models": models or self._models(),
              "mic": self.mic_info() if self.voice else {}, "files_denied": self._files_denied(),
              "app": responsible_app()}
        return health.issues(st, self.s.data_dir, self.perf.stats)

    def _start_memory(self) -> None:
        m = self.memory
        if m is None:
            return
        try:
            m.start()
            if m.semantic:
                self.bus.log(f"Memory recall ready ({self.s.embed_model})")
            elif not self.s.embed_model:
                self.bus.log("Memory recall by word match (keeps JARVIS light)")
            else:
                self.bus.log(f"Memory recall by word match only: {m.embed_error}", "warn")
        except Exception:
            log.exception("memory start failed")

    def stop(self) -> None:
        with self._life:
            self._stop.set()
        if self._announce is not None:
            self._announce.cancel()
        self.perf.stop()
        if self._thread:
            self._thread.join(timeout=2)
        self.camera.stop()
        if self.voice is not None:
            self.voice.stop()
        if self.speech is not None:
            self.speech.stop()
        if self.brain is not None:
            self.brain.stop()
        if self.alarms is not None:
            self.alarms.stop()
        self.cancel_delayed()

    # -- modes ---------------------------------------------------------------
    def begin_enrollment(self) -> None:
        with self._lock:
            self.enrollment = EnrollmentSession(
                min_quality=self.s.min_face_quality,
                min_live_score=self.s.liveness_spoof_threshold if self.s.liveness_enabled else None,
            )
            self.mode = "enrolling"
        self.bus.log("Face enrollment started")

    def cancel_enrollment(self) -> None:
        with self._lock:
            self.enrollment = None
            self.mode = "verifying" if self.verifier else "idle"
        self.bus.log("Face enrollment cancelled", "warn")

    def end_rescan(self) -> None:
        """Give up a re-scan and keep the current face profile."""
        if self.face_enrolled:
            self.cancel_enrollment()
            self._reenroll = None

    def begin_verification(self) -> bool:
        if not self.face_on:
            with self._lock:
                self.mode = "verifying"
            if self.voice is not None and self.voice.enrolled:
                self.voice.begin_verification()
            self.bus.log("Voice sign-in active (face sign-in is off)")
            return True
        try:
            template = self.store.load(FACE)
        except Exception:
            log.exception("could not decrypt face template")
            self.bus.log("Face profile could not be decrypted", "error")
            return False
        if template is None:
            return False
        with self._lock:
            self.verifier = TemplateMatcher(template, top_k=self.s.face_top_k)
            self.auth = self._new_auth()
            self.live = self._new_gate()
            self._unlocked = False
            self.mode = "verifying"
        self.bus.log("Continuous face verification active")
        if self.voice is not None and self.voice.enrolled:
            self.voice.begin_verification()
        return True

    # -- main loop -----------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            # blinks last ~150 ms: analyse faster while a challenge is running
            fps = self.s.challenge_fps if self.live.state == "challenge" else self.s.process_fps
            if self.mode == "verifying" and self.live.state != "challenge" and time.monotonic() - self._last_face_t > NO_FACE_IDLE_S:
                fps = min(fps, IDLE_FPS)
            period = 1.0 / max(fps, 1.0)
            t0 = time.monotonic()
            try:
                self._tick(t0)
            except Exception:
                log.exception("face loop tick failed")
            self._stop.wait(max(0.0, period - (time.monotonic() - t0)))

    def _preview_loop(self) -> None:
        """The camera view on screen, at its own steady rate with the latest face boxes, so
        it stays smooth however long each face analysis takes."""
        period = 1.0 / PREVIEW_FPS
        while not self._stop.wait(period):
            frame = self.camera.latest()
            if frame is not None and self.bus.has_subscribers:
                try:
                    self._push_preview(frame, self._boxes)
                except Exception:
                    log.exception("preview failed")

    def _tick(self, now: float) -> None:
        frame = self.camera.latest()
        if frame is None or not self.engine.ready or self.mode == "idle" or self._face_session is not None:
            self._boxes = []  # (face once: the camera is off; nothing to analyse until the session ends)
            return
        if self.mode == "verifying":
            frozen = self.frozen.update(frame, now)
            if frozen and not self._frozen:
                self.db.add_security_event(
                    "camera_frozen", "Camera delivered identical frames — virtual or replayed feed?", blocked=True
                )
                self.bus.log("Camera feed frozen — not accepted as live", "alert")
            self._frozen = frozen
        faces = self.engine.analyze(frame)
        if faces:
            self._last_face_t = time.monotonic()
        h, w = frame.shape[:2]
        self._boxes = [[float(v) for v in (f.bbox / np.array([w, h, w, h]))] for f in faces]
        if self.mode == "enrolling":
            self._tick_enrollment(faces, now)
        elif self.mode == "verifying":
            self._tick_verification(faces, now)

    def _tick_enrollment(self, faces: list[FaceObservation], now: float) -> None:
        with self._lock:
            session = self.enrollment
        if session is None:
            return
        session.update(faces, now)
        self.bus.publish({"type": "enroll", **session.snapshot()})
        if session.done:
            with self._lock:  # cancel / delete / factory reset take this lock too
                if self.enrollment is not session:
                    return  # cancelled while this frame was processed: don't save
                template = session.template()
                self.store.save(FACE, template)
                self.enrollment = None
                self.mode = "idle"
            quality = TemplateMatcher(template, self.s.face_top_k).self_consistency()
            log.info("face template saved: %d samples, self-consistency %.3f", len(template), quality)
            self.bus.log(f"Face profile saved ({len(template)} encrypted samples)")
            self.bus.publish({"type": "enroll_complete"})
            if self.setup_complete:  # a re-scan: back to continuous verification with the new face
                self._reenroll = None
                self.db.add_security_event("face_reenrolled", "Face profile replaced by a new scan")
                self.begin_verification()

    def _tick_verification(self, faces: list[FaceObservation], now: float) -> None:
        verifier = self.verifier
        if verifier is None:
            return
        usable = [f for f in faces if f.quality >= self.s.min_face_quality]
        sims = [verifier.similarity(f.embedding) for f in usable]
        prev = self.auth.state
        prev_effective = self.effective_state()
        events = self.auth.update(
            FrameResult(similarities=sims, low_quality_faces=len(faces) - len(usable)), now
        )
        for kind, detail in events:
            self._handle_event(kind, detail, prev, sims)

        self._face_quality = usable[int(np.argmax(sims))].quality if sims else None

        live_events: list[tuple[str, str]] = []
        if self.s.liveness_enabled:
            if self._demand_liveness:
                reason, self._demand_liveness = self._demand_liveness, None
                live_events += self.live.demand(now, reason)
            obs = None
            if sims and max(sims) >= self.s.face_reject_threshold:
                best = usable[int(np.argmax(sims))]
                obs = LiveObs(
                    yaw=best.yaw,
                    rel_width=best.rel_width,
                    center=best.center,
                    eye_open=best.eye_open,
                    live_score=best.live_score,
                )
            live_events += self.live.update(self.auth.state, obs, now, frozen=self._frozen)
            for kind, detail in live_events:
                self._handle_liveness(kind, detail, sims)

        self._expire_pending(now)
        if self.prefs.get("security.camera") == "once" and self._face_session is None \
                and self.effective_state() == "approved" and not self.auth.snapshot().bystander \
                and self.trust(now).level >= 1:  # the owner alone in view: the camera then stops watching
            self._close_camera_after_face(usable, sims)
            return
        changed = self.effective_state() != prev_effective
        # challenges push faster so the countdown and hints stay live
        interval = 0.25 if self.live.state == "challenge" else 1.0
        if events or live_events or changed or now - self._last_state_push > interval:
            self._push_state()

    def _handle_event(self, kind: str, detail: str, prev: str, sims: list[float]) -> None:
        if kind == "state_approved":
            if not self.s.liveness_enabled:
                self.bus.log("Face verified — owner identified", "ok")
                if prev != "approved":
                    self._greet()
            elif self.live.state != "passed":
                self.bus.log("Face matched — verifying liveness")
        elif kind == "state_denied":
            best = max(sims) if sims else None
            self.db.add_security_event(
                "unknown_face",
                "Unrecognised person in front of the camera",
                face_conf=None if best is None else round(confidence(best, self.s.face_threshold), 3),
                blocked=True,
            )
            self.bus.log("Unknown person detected — access denied", "alert")
            self.record_sample(0, "stranger")
            self.bus.publish(
                {"type": "say", "text": "Authentication failed. You are not my boss."}
            )
        elif kind == "state_absent":
            self._unlocked = False
            if self.voice is not None:
                self.voice.auth.reset()  # a new presence session starts without voice evidence
            if self.brain is not None:
                self.brain.clear()
            self.bus.log("Owner left — session locked", "warn")
        elif kind == "state_scanning":
            self.bus.log(detail)
        elif kind == "bystander":
            self.db.add_security_event("bystander", detail)
            self.bus.log("Unknown person in view with owner", "warn")

    # -- commands --------------------------------------------------------------
    def command(self, text: str, lang: str = "en", source: str = "voice", verdict: str | None = None) -> dict:
        """Run one owner command through the brain and answer (shown + spoken).

        Callers have already checked level 1 (the live owner is at the
        screen). Each proposed action is checked against the level it needs
        when it runs; deletions become a pending confirmation. ``verdict``: the voice match of
        this very utterance (spoken commands); anything beyond level 1 needs "verified".
        """
        text = " ".join(text.split())[:500]
        self.bus.publish({"type": "heard", "text": text, "lang": lang, "source": source})
        if source == "typed" and self.prefs.get("security.typed") == "off" and not self.everyday(text):
            # "Voice only": the keyboard would bypass the voice check, so typing gets the harmless
            # everyday commands and nothing that reads your data, asks the AI or acts
            reply = self._blocked_say("voice_needed", lang, "typed")
            self.bus.publish({"type": "reply", "text": reply, "actions": []})
            self.bus.publish({"type": "say", "text": reply})
            return {"reply": reply, "language": lang, "actions": [], "ok": False}
        if self.brain is None:
            reply = "My language model is turned off."
            result = {"reply": reply, "language": lang, "actions": [], "ok": False}
        else:
            self.bus.publish({"type": "thinking", "active": True})
            with self._command_lock:  # one at a time; the model is the bottleneck
                r = self.brain.respond(text, lang)
            self.bus.publish({"type": "thinking", "active": False})
            actions: list[dict[str, Any]] = [{"tool": a.tool, "args": a.args, "summary": a.summary} for a in r.actions]
            reply = r.reply
            if r.actions and self.tools is not None:
                with self._tools_lock:
                    results = self._run_tools(r.actions, r.language, source, verdict=verdict)
                for info, res in zip(actions, results):
                    info.update(ok=res.ok, result=res.say, data=res.data)
                reply = " ".join(res.say for res in results)
                note = getattr(self.brain, "note_result", None)
                if note is not None:
                    note(reply)
            result = {"reply": reply, "language": r.language, "actions": actions, "ok": r.ok, "latency_s": round(r.latency_s, 2)}
            kinds = ", ".join(a.tool for a in r.actions) or "conversation"
            how = "instantly" if r.fast else f"in {r.latency_s:.1f} s"
            self.bus.log(f"Command understood ({kinds}) {how}" if r.ok else "Language model unavailable", "info" if r.ok else "error")
        self.bus.publish({"type": "reply", "text": result["reply"], "actions": result["actions"]})
        self.bus.publish({"type": "say", "text": result["reply"]})
        if self.memory is not None and result.get("ok", True) and self.prefs.get("memory.enabled"):
            # after the reply: history and memory suggestions never slow down the answer
            chat = not result["actions"]
            threading.Thread(target=self._after_turn, args=(text, result["reply"], result["language"], chat),
                             name="memory-turn", daemon=True).start()
        return result

    def _after_turn(self, text: str, reply: str, lang: str, chat: bool) -> None:
        m = self.memory
        if m is None:
            return
        try:
            m.log_turn(text, reply, lang, embed=chat)  # commands: no recall model load
            if chat and self.suggestions_on() and m.sounds_personal(text):
                for sug in m.suggest(text):
                    self.bus.publish({"type": "memory_suggestion", "id": sug.id, "text": sug.text})
                    self.bus.log("Memory suggestion ready")
        except Exception:
            log.exception("memory bookkeeping failed")

    def memory_changed(self) -> None:
        self.bus.publish({"type": "memory_changed"})

    def accept_suggestion(self, sid: str, accept: bool) -> dict:
        m = self.memory
        s = m.pop_suggestion(sid) if m else None
        if m is None or s is None:
            return {"ok": False}
        if accept:
            fact, _ = m.remember(s.text, "suggested")
            self.bus.log("Saved to memory", "ok")
            return {"ok": True, "id": fact.id if fact else None}
        return {"ok": True}

    def everyday(self, text: str) -> bool:
        """A harmless everyday command (brightness, volume, a timer, music, a screenshot, a web
        search, the time…): understood without the language model and harmless if someone else
        said it, so it runs when the voice match is only uncertain (a short "louder")."""
        b = self.brain
        if b is None or self.tools is None:
            return False
        actions = b.fast_actions(text)
        if actions is None:
            return False
        return all(a.tool in HARMLESS for a in actions)  # [] = the time, a date or a sum

    def _blocked_say(self, code: str, lang: str, source: str) -> str:
        if code == "voice_needed" and source == "voice":
            code = "voice_needed_voice"  # they did speak; the match was just unclear
        elif code == "voice_needed" and source in ("typed", "click"):
            code = "voice_needed_typed"  # typed commands are switched off in Settings
        en, hi = BLOCKED_SAY.get(code, (REASONS.get(code, "Not allowed right now."),) * 2)
        return hi if lang != "en" else en

    def _run_tools(self, actions, lang: str, source: str = "voice", granted: int = 0,
                   verdict: str | None = None) -> list[ToolResult]:
        """``granted``: the level the owner had when a delayed request was made (it still stops
        if they are no longer verified at all). ``verdict``: the voice match of the utterance
        that asked; anything beyond level 1 needs it to be "verified" (a match a minute ago isn't
        enough: every request that reads mail or acts is checked on its own words)."""
        hi = lang != "en"
        results: list[ToolResult] = []
        did_level2 = False
        for i, a in enumerate(actions):
            need = LEVELS.get(a.tool, 3)
            trust = self.trust(screen=source == "typed")  # re-checked per action: the owner may have left meanwhile
            if trust.level == 0:
                results.append(ToolResult(a.tool, False, "रुक गया: आप अब सत्यापित नहीं हैं।" if hi
                                          else "Stopped: you're no longer verified."))
                self.db.add_security_event("tool_blocked", f"{a.tool} blocked: owner no longer verified", blocked=True)
                break
            unverified = source == "voice" and not granted and verdict is not None and verdict != "verified"
            if a.tool == "wait":
                # what runs later was granted now: an unconfirmed voice grants level 1 at most
                level = 1 if unverified else max(trust.level, granted)
                results.append(self._delay(a, list(actions[i + 1:]), lang, source, level))
                break  # the rest runs when the delay is over
            if need >= 2 and (unverified or max(trust.level, min(granted, 2)) < 2):
                code = "voice_needed" if unverified else trust.blockers.get(2, "low_confidence")
                results.append(ToolResult(a.tool, False, self._blocked_say(code, lang, source), {"blocked": code}))
                self.db.add_security_event(
                    "tool_blocked", f"{a.tool} needs level {need}, have {trust.level}: {REASONS.get(code, code)}", blocked=True
                )
                self.bus.log(f"Tool {a.tool} blocked — {REASONS.get(code, code)}", "warn")
                continue
            if a.tool == "mac.do":
                results.append(self._script(a, lang, trust))
                continue
            if need == 3:
                results.append(self._request_confirmation(a, lang, trust))
                continue
            assert self.tools is not None  # actions only run when tools are on
            res = self.tools.run(a, lang)
            self.bus.log(f"Tool {a.tool}: {'done' if res.ok else 'failed'}", "ok" if res.ok else "warn")
            results.append(res)
            did_level2 |= res.ok and need >= 2
            if res.ok and a.tool == "system.lock":
                self.end_face_session("Screen locked")
        if did_level2:
            self.record_sample(1, "owner_command")
        return results

    # -- delays ("close it after 10 seconds") ------------------------------------------
    def _delay(self, wait, rest: list, lang: str, source: str, level: int) -> ToolResult:
        hi = lang != "en"
        try:
            secs = int(wait.args.get("seconds"))
        except (TypeError, ValueError):
            secs = 0
        if not rest:
            return ToolResult("wait", False, "किसके लिए रुकूँ, समझ नहीं आया।" if hi else "I didn't catch what to do after the wait.")
        if not 1 <= secs <= MAX_DELAY_S:
            return ToolResult("wait", False, "इतनी देर बाद के लिए अलार्म लगाइए।" if hi
                              else "That delay is too long; set an alarm or a timer instead.")
        need = max(min(LEVELS.get(a.tool, 3), 2) for a in rest)
        if level < need:
            code = self.trust(screen=source == "typed").blockers.get(need, "voice_needed")
            return ToolResult("wait", False, self._blocked_say(code, lang, source), {"blocked": code})
        what = ", ".join(a.summary or a.tool for a in rest)
        did = secrets.token_hex(4)
        timer = threading.Timer(secs, self._fire_delayed, args=(did, rest, lang, source, level))
        timer.daemon = True
        with self._delayed_lock:
            self._delayed[did] = Delayed(did, time.time() + secs, what, timer)
        timer.start()
        self.tools_changed()
        self.bus.log(f"Scheduled in {secs} s: {what}")
        say = (f"{wait_phrase(secs, True)} बाद: {what}।" if hi else f"In {wait_phrase(secs, False)}: {what}.")
        return ToolResult("wait", True, say, {"delayed": did, "seconds": secs})

    def _fire_delayed(self, did: str, rest: list, lang: str, source: str, level: int) -> None:
        with self._delayed_lock:
            if self._delayed.pop(did, None) is None:
                return  # cancelled
        self.tools_changed()
        try:
            with self._tools_lock:
                results = self._run_tools(rest, lang, source, granted=level)
        except Exception:
            log.exception("delayed actions failed")
            return
        actions = [{"tool": a.tool, "args": a.args, "summary": a.summary, "ok": r.ok, "result": r.say, "data": r.data}
                   for a, r in zip(rest, results)]
        text = " ".join(r.say for r in results)
        if text:
            self.bus.publish({"type": "reply", "text": text, "actions": actions})
            self.bus.publish({"type": "say", "text": text})

    def delayed(self) -> list[dict]:
        with self._delayed_lock:
            items = sorted(self._delayed.values(), key=lambda d: d.due)
        return [{"id": d.id, "due": d.due, "summary": d.summary} for d in items]

    def cancel_delayed(self, did: str | None = None) -> int:
        with self._delayed_lock:
            gone = [d for k, d in self._delayed.items() if did is None or k == did]
            for d in gone:
                self._delayed.pop(d.id, None)
        for d in gone:
            d.timer.cancel()
        if gone:
            self.tools_changed()
        return len(gone)

    # -- any command: generated AppleScript ------------------------------------------------
    def scripts_always_ask(self) -> bool:
        """8 GB Macs run the small model, which makes more mistakes: every script is shown first."""
        return self.prefs.get("security.scripts") == "always" or profile().name == "small"

    def _script(self, action, lang: str, trust: Trust) -> ToolResult:
        hi = lang != "en"
        assert self.tools is not None
        if self.prefs.get("security.scripts") == "off":
            return ToolResult("mac.do", False, "जनरेट की गई स्क्रिप्ट सेटिंग्स में बंद हैं।" if hi
                              else "That isn't built in, and generated scripts are turned off in Settings → Commands.")
        if self.pending() is not None:
            return ToolResult("mac.do", False, "पहले पिछली पुष्टि पूरी कीजिए।" if hi else "Answer the waiting confirmation first, please.")
        self.bus.publish({"type": "thinking", "active": True})
        try:
            plan = self.tools.plan(action, lang)
        finally:
            self.bus.publish({"type": "thinking", "active": False})
        if isinstance(plan, ToolResult):
            if plan.data.get("blocked") == "script":
                self.db.add_security_event("tool_blocked", f"generated script refused: {plan.say}", blocked=True)
            return plan
        assert plan.script is not None
        if plan.script.verdict == "read" and not self.scripts_always_ask():
            res = self.tools.execute(plan)
            self.bus.log(f"Script (read only): {'done' if res.ok else 'failed'}", "ok" if res.ok else "warn")
            return res
        what = plan.what[:1].lower() + plan.what[1:]
        say = (f"यह स्क्रिप्ट चलाऊँ? {plan.what}। “हाँ, कर दो” बोलिए या Confirm दबाइए।" if hi
               else f"Shall I run this script? It {what.rstrip('.')}. Say “yes, go ahead” or click Confirm.")
        return self._open_pending(plan, lang, say, trust)

    # -- confirmations (level 3) ------------------------------------------------------
    def pending(self) -> Pending | None:
        p = self._pending
        return p if p is not None and self.clock() < p.expires else None

    def _finish_pending(self, outcome: str, only: Pending | None = None) -> Pending | None:
        """Close the pending action. With ``only``, close it only if it is still that one:
        of two racing confirmations exactly one gets it (and runs it)."""
        with self._pending_lock:
            if only is not None and self._pending is not only:
                return None
            p, self._pending = self._pending, None
        if p is not None:
            self.bus.publish({"type": "confirm_done", "id": p.id, "outcome": outcome})
        return p

    def _expire_pending(self, now: float) -> None:
        p = self._pending
        if p is None:
            return
        if now >= p.expires:
            self._finish_pending("expired")
            self.bus.log("Not confirmed in time — cancelled", "warn")
        elif self.trust(now).level == 0 and not (self.s.liveness_enabled and self.live.state == "challenge"):
            # (during a liveness challenge the level is 0 until it passes: often the very check
            # this confirmation asked for; a failed one, or the owner leaving, still cancels)
            self._finish_pending("cancelled")
            self.bus.log("Pending confirmation cancelled — owner no longer verified", "warn")

    def _request_confirmation(self, action, lang: str, trust: Trust) -> ToolResult:
        hi = lang != "en"
        if self.pending() is not None:
            return ToolResult(action.tool, False, "एक बार में एक ही चीज़ हटा सकती हूँ।" if hi else "One deletion at a time, please.")
        assert self.tools is not None
        plan = self.tools.plan(action, lang)
        if isinstance(plan, ToolResult):
            return plan
        if plan.ask:  # its own question (an email is read back in full before it is sent)
            return self._open_pending(plan, lang, plan.ask, trust)
        if plan.tool == "files.trash":
            ask = f"{plan.what} ट्रैश में डाल दूँ?" if hi else f"Move {plan.what} to the Trash?"
        else:
            ask = f"{plan.what} हटा दूँ?" if hi else f"Delete {plan.what}?"
        say = f"{ask} “हाँ, कर दो” बोलिए या Confirm दबाइए।" if hi else f"{ask} Say “yes, go ahead” or click Confirm."
        return self._open_pending(plan, lang, say, trust)

    def request_sensitive(self, tool: str, what: str, question: str, run: Callable[[], ToolResult],
                          lang: str = "en") -> dict:
        """Open a level-3 confirmation for a privacy/settings action (callers checked level 2)."""
        if self.pending() is not None:
            return {"ok": False, "reply": "Another confirmation is already waiting."}
        say = f"{question} Say “yes, go ahead” or click Confirm."
        res = self._open_pending(Plan(tool, 0, what, lang != "en"), lang, say, self.trust(), run)
        self.bus.publish({"type": "say", "text": res.say})
        return {"ok": True, "reply": res.say, "pending": res.data.get("pending")}

    def _open_pending(self, plan: Plan, lang: str, say: str, trust: Trust,
                      run: Callable[[], ToolResult] | None = None) -> ToolResult:
        # a long question (an email read back aloud) takes a while to hear before it can be answered
        ttl = self.s.confirm_ttl_s + max(0, len(say.split()) - 20) / 2.5
        if trust.needs_fresh_liveness:
            self._demand_liveness = "Fresh liveness check before a deletion"
            ttl += 20.0  # time for the challenge
            say = self._blocked_say("fresh_liveness", lang, "voice") + " " + say
        pid = secrets.token_hex(4)
        with self._pending_lock:
            self._pending = Pending(pid, plan, lang, say, self.clock() + ttl, run)
        detail = plan.script.script if plan.script is not None else plan.detail
        self.bus.publish({"type": "confirm", "id": pid, "tool": plan.tool, "text": say, "expires_s": ttl, "detail": detail})
        self.bus.log(f"Waiting for confirmation: {plan.tool}")
        return ToolResult(plan.tool, True, say, {"pending": pid})

    def confirm_spoken(self, accept: bool, verdict: str | None) -> None:
        p = self.pending()
        if p is not None:
            self.confirm(p.id, accept, "voice", verdict)

    def confirm(self, pid: str, accept: bool, source: str, verdict: str | None = None) -> dict:
        p = self.pending()
        hi = p is not None and p.lang != "en"
        if p is None or p.id != pid:
            return self._answer("Nothing is waiting for confirmation.", ok=False)
        if not accept:
            if self._finish_pending("cancelled", p) is None:
                return self._answer("Nothing is waiting for confirmation.", ok=False)
            self.bus.log("Cancelled by the owner")
            if p.run is not None:
                return self._answer("ठीक है, कुछ नहीं बदला।" if hi else "Okay, nothing changed.", ok=True)
            if p.plan.tool == "mac.do":  # a generated script, not a deletion
                return self._answer("ठीक है, स्क्रिप्ट नहीं चलाई।" if hi else "Okay, I won't run it.", ok=True)
            if p.plan.tool == "email.send":
                return self._answer("ठीक है, नहीं भेजा। ड्राफ़्ट Gmail में है।" if hi
                                    else "Okay, not sent. The draft stays in Gmail.", ok=True)
            return self._answer("ठीक है, नहीं हटाया।" if hi else "Okay, I won't delete it.", ok=True)
        if source == "voice" and verdict != "verified":
            # a short "yes" carries too little voice to identify the speaker
            return self._answer(
                "आपकी आवाज़ पक्की नहीं हो पाई। “हाँ, कर दो” साफ़ बोलिए या Confirm दबाइए।" if hi
                else "I couldn't verify your voice from that. Say “yes, go ahead” clearly, or click Confirm.",
                ok=False,
            )
        trust = self.trust(screen=source == "click")
        if not trust.l3_ready:
            code = trust.blockers.get(3, "low_confidence")
            if code == "fresh_liveness":
                if self.live.state != "challenge":
                    self._demand_liveness = "Fresh liveness check before a deletion"
                    with self._pending_lock:
                        if self._pending is p:
                            p.expires = max(p.expires, self.clock() + 25.0)
                text = "पहले लाइवनेस चेक पूरा कीजिए, फिर पुष्टि कीजिए।" if hi else "Finish the liveness check first, then confirm."
            else:
                text = self._blocked_say(code, p.lang, source)
                self.db.add_security_event("tool_blocked", f"{p.plan.tool} confirmation refused: {REASONS.get(code, code)}", blocked=True)
            return self._answer(text, ok=False)
        if self._finish_pending("done", p) is None:  # another confirmation got there first
            return self._answer("Nothing is waiting for confirmation.", ok=False)
        try:
            if p.run is not None:
                res = p.run()
            else:
                assert self.tools is not None  # a tool plan exists only when tools are on
                res = self.tools.execute(p.plan)
        except Exception as exc:
            log.exception("confirmed action %s failed", p.plan.tool)
            res = ToolResult(p.plan.tool, False, f"That failed: {exc}")
        if p.run is None:  # privacy/settings actions log their own, more specific event
            self.db.add_security_event("sensitive_action", f"{p.plan.tool} confirmed by {'voice' if source == 'voice' else 'click'}")
        self.bus.log(f"Tool {p.plan.tool}: {'done' if res.ok else 'failed'} (confirmed)", "ok" if res.ok else "warn")
        self.record_sample(1, "owner_confirm")
        action = {"tool": p.plan.tool, "args": {}, "summary": p.plan.what, "ok": res.ok, "result": res.say, "data": res.data}
        return self._answer(res.say, ok=res.ok, actions=[action])

    def _answer(self, text: str, ok: bool, actions: list | None = None) -> dict:
        self.bus.publish({"type": "reply", "text": text, "actions": actions or []})
        self.bus.publish({"type": "say", "text": text})
        return {"ok": ok, "reply": text}

    # -- privacy ------------------------------------------------------------------
    def request_privacy(self, action: str, lang: str = "en", path: str | None = None) -> dict:
        """Open the level-3 confirmation for a privacy action (callers checked level 2)."""
        what, question = privacy.ACTIONS[action]
        if action == "export":
            target = privacy.validate_export_path(path or "", self.s.data_dir)
            run = lambda: self._export(target)
            question = f"Export your data to {target.name}? It will be readable, not encrypted."
        elif action == "reenroll_face":
            run = lambda: self._grant_reenroll("redo")
        else:
            run = getattr(self, f"_{action}")
        return self.request_sensitive(f"privacy.{action}", what, question, run, lang)

    def _done(self, action: str, say: str, detail: str | None = None, **data) -> ToolResult:
        self.db.add_security_event("privacy_action", detail or say)
        self.bus.log(say, "ok")
        self.bus.publish({"type": "privacy_changed"})
        return ToolResult(f"privacy.{action}", True, say, data)

    def _grant_reenroll(self, kind: str) -> ToolResult:
        self.end_face_session("Face re-scan")  # face once: the scan needs the camera back on
        self._reenroll = (kind, self.clock() + REENROLL_GRANT_S)
        self.bus.publish({"type": "face_reenroll", "kind": kind})
        return self._done("reenroll_face", "Okay — look at the camera and follow the prompts to re-scan your face.",
                          "Face re-scan authorised")

    def face_enroll_allowed(self) -> tuple[bool, str]:
        """May someone start a face scan right now?"""
        if not self.face_on:
            return False, "Face sign-in is turned off"
        if not self.setup_complete:
            return True, "first-time setup"
        g = self._reenroll
        if g is not None and self.clock() < g[1]:
            return True, f"re-scan authorised ({g[0]})"
        if not self.face_enrolled:
            v = self.voice
            last = v.auth.last_verified if v is not None and v.mode == "verifying" else None
            if v is not None and last is not None and self.clock() - last.t <= RECOVERY_VOICE_S \
                    and not v.auth.rejected_since_verified:
                return True, "voice verified"
            if v is not None and v.enrolled:
                return False, f"Say “{self.assistant_name}, it's me” so I can recognise your voice first"
            return False, "No face or voice profile left — use Factory reset to set up again"
        return False, "Re-scanning needs a confirmation (Settings → Identity → Re-scan face)"

    def locked_out(self) -> bool:
        """Set up, but no face profile, no usable voice profile and no re-scan window:
        nobody can ever be verified again, so a reset must be possible without it."""
        if not self.face_on or not self.setup_complete or self.face_enrolled:
            return False
        g = self._reenroll
        if g is not None and self.clock() < g[1]:
            return False
        v = self.voice
        return v is None or not v.enrolled or not v.available

    def _unverified_reply(self, verdict: str | None) -> str | None:
        """What to say to an addressed command while nobody is verified (None = default)."""
        if not (self.face_on and self.setup_complete and not self.face_enrolled):
            return None
        if verdict == "verified":
            self.bus.publish({"type": "face_reenroll", "kind": "voice"})
            return "I recognise your voice. You can scan your face now."
        return "I need to recognise your voice first. Please say that again, a little longer."

    def _stop_face(self) -> None:
        if self._face_session is not None:
            # face once had switched the camera off; the next scan (or setup) needs it
            self._face_session = None
            self.camera.start()
        with self._lock:
            self.enrollment = None
            self.verifier = None
            self.auth = self._new_auth()
            self.live = self._new_gate()
            self._unlocked = False
            self.mode = "idle"
        if self.brain is not None:
            self.brain.clear()

    def _close_camera_after_face(self, usable: list[FaceObservation], sims: list[float]) -> None:
        """Face once mode: the owner's face and liveness are proven for this launch. Keep that
        result, switch the camera off, and let the voice carry every later command."""
        best = usable[int(np.argmax(sims))] if sims else None
        self._face_session = {
            "sim": self.auth.smoothed,
            "quality": self._face_quality,
            "live_score": self.live.live_score if self.s.liveness_enabled else None,
            "confidence_sim": self.auth.snapshot().confidence_sim,
            "t": self.clock(),
        }
        self._boxes = []
        threading.Thread(target=self.camera.stop, name="camera-off", daemon=True).start()
        self.db.add_security_event("face_session", "Face verified at launch; camera off, voice from here on")
        self.bus.log("Face verified — camera off until JARVIS restarts; your voice keeps you signed in", "ok")
        if best is not None and not self._unlocked:
            self._greet()
        self._push_state()

    def end_face_session(self, reason: str) -> None:
        """Back to the camera: the next command needs a fresh face check."""
        if self._face_session is None:
            return
        self.bus.log(f"{reason} — face check needed again", "warn")
        self.camera.start()
        # fresh face state first, then drop the launch result: in between, the stale
        # "approved" left from launch must not count
        if self.mode == "verifying" or (self.setup_complete and self.face_enrolled):
            self.begin_verification()
        self._face_session = None
        self._push_state()

    def _delete_face(self) -> ToolResult:
        self._stop_face()
        self.store.delete(FACE)
        self._grant_reenroll("deleted")
        self._push_state()
        return self._done("delete_face", "Face profile deleted. Look at the camera to scan your face again.",
                          "Face profile deleted")

    def _delete_voice(self) -> ToolResult:
        v = self.voice
        if v is not None:
            v.cancel_enrollment("Voice enrollment stopped — profile deleted")
            with v._lock:
                v.matcher = None
                v.mode = "idle"
            v.auth.reset()
        self.store.delete("voice")
        return self._done("delete_voice", "Voice profile deleted. You can enroll again from Authentication.")

    def _clear_memory(self) -> ToolResult:
        n = self.memory.clear_facts() if self.memory else 0
        return self._done("clear_memory", f"Forgot everything I remembered ({n} facts).")

    def _clear_history(self) -> ToolResult:
        n = self.memory.clear_history() if self.memory else 0
        return self._done("clear_history", f"Conversation history deleted ({n} messages).")

    def _clear_tools(self) -> ToolResult:
        if self.alarms is not None:
            self.alarms.dismiss()
        n = sum(len(self.db.run(f"DELETE FROM {t} RETURNING id")) for t in ("notes", "alarms", "events")
                if t in self.db.tables())
        self.tools_changed()
        return self._done("clear_tools", f"Notes, alarms and events deleted ({n} items).")

    def _clear_security_log(self) -> ToolResult:
        n = self.db.clear_security_events()
        return self._done("clear_security_log", f"Security log cleared ({n} events).", "Security log cleared")

    def _reset_fusion(self) -> ToolResult:
        self.samples.clear()
        self.reset_fusion()
        return self._done("reset_fusion", "Fusion model reset and its device samples deleted.")

    def _export(self, path) -> ToolResult:
        n = privacy.write_export(self, path)
        return self._done("export", f"Exported your data to {path.name} ({n // 1024 + 1} KB).",
                          f"Data exported to {path}", path=str(path))

    def _factory_reset(self) -> ToolResult:
        self._stop_face()
        self._reenroll = None
        if self.voice is not None:
            self.voice.cancel_enrollment("Voice enrollment stopped — factory reset")
            with self.voice._lock:
                self.voice.matcher = None
                self.voice.mode = "idle"
            self.voice.auth.reset()
        if self.alarms is not None:
            self.alarms.dismiss()
        if self.google is not None and self.google.connected:
            try:
                self.google.disconnect()  # revoked at Google too, not just forgotten here
            except Exception:
                log.exception("google disconnect during reset failed")
        for f in self.s.templates_dir.glob("*"):
            f.unlink(missing_ok=True)
        self.s.fusion_model_path.unlink(missing_ok=True)
        self.fusion, self.fusion_source = load_model(None) if self.s.fusion_enabled else (None, "disabled")
        self.db.wipe()
        self.store.keys.delete_key()  # crypto-erase: anything left on disk can't be decrypted
        if self.memory is not None:
            self.memory.reset_cache()
        self._apply_security_settings()
        self._apply_voice_settings()
        self.apply_mode()
        self.db.add_security_event("factory_reset", "All data erased; encryption key destroyed")
        self.bus.log("Factory reset complete — starting setup", "ok")
        self.bus.publish({"type": "privacy_changed"})
        self.bus.publish({"type": "status", **self.status()})
        return ToolResult("privacy.factory_reset", True, "Everything is erased. Let's set things up again.")

    # -- alarms ------------------------------------------------------------------
    def tools_changed(self) -> None:
        self.bus.publish({"type": "tools_changed"})

    def ring(self, alarm: Alarm, count: int) -> None:
        owner = self.owner_name
        label = f" {alarm.label}." if alarm.label else ""
        if alarm.kind == "timer":
            text = f"{owner}, your timer is done.{label}"
        else:
            text = f"{owner}, it's {alarm.due.strftime('%I:%M %p').lstrip('0')}. Your alarm is ringing.{label}"
        self.bus.publish({"type": "alarm", "id": alarm.id, "kind": alarm.kind, "label": alarm.label,
                          "due": alarm.due.isoformat(timespec="minutes"), "count": count})
        if self.speech is not None:
            self.speech.out.chime()
        self.bus.publish({"type": "say", "text": text})
        if count == 0:
            self.bus.log(f"{alarm.kind.capitalize()} ringing", "warn")
            notify(self.assistant_name, text)

    def dismiss_alarms(self) -> int:
        n = self.alarms.dismiss() if self.alarms else 0
        if n:
            if self.speech is not None:
                self.speech.out.interrupt()
            self.bus.publish({"type": "alarm_stopped"})
            self.bus.log("Alarm dismissed")
        return n

    def snooze_alarms(self, minutes: int = 5) -> int:
        n = self.alarms.snooze(minutes) if self.alarms else 0
        if n:
            if self.speech is not None:
                self.speech.out.interrupt()
            self.bus.publish({"type": "alarm_stopped"})
            self.bus.log(f"Alarm snoozed for {minutes} min")
        return n

    def _greet(self) -> None:
        self._unlocked = True
        greeting = f"Authentication approved. Hi {self.owner_name}, how may I help you today?"
        self.bus.publish({"type": "say", "text": greeting})

    def _face_conf(self, sims: list[float]) -> float | None:
        return round(confidence(max(sims), self.s.face_threshold), 3) if sims else None

    def _handle_liveness(self, kind: str, detail: str, sims: list[float]) -> None:
        if kind == "liveness_challenge":
            self.bus.log(f"{detail} — challenge issued")
            self.bus.publish({"type": "say", "text": "Quick liveness check. Follow the prompts."})
        elif kind == "liveness_passed":
            self.bus.log("Liveness confirmed — live person", "ok")
            if not self._unlocked:
                self._greet()
        elif kind == "liveness_failed":
            self.db.add_security_event(
                "liveness_failed", detail, face_conf=self._face_conf(sims), blocked=True
            )
            self.bus.log(f"Liveness check failed: {detail}", "alert")
        elif kind == "liveness_lockout":
            self.db.add_security_event("liveness_lockout", detail, blocked=True)
            self.bus.log(detail, "alert")
            self.bus.publish(
                {"type": "say", "text": "Too many failed liveness checks. Access is locked for a minute."}
            )
        elif kind == "spoof":
            self._unlocked = False
            self.db.add_security_event(
                "spoof_suspected", detail, face_conf=self._face_conf(sims), blocked=True
            )
            self.bus.log(f"Spoof suspected: {detail}", "alert")
            self.record_sample(0, "spoof")
            self.bus.publish({"type": "say", "text": "Spoof attempt detected. Access denied."})
        elif kind == "liveness_reset":
            self._unlocked = False

    # -- outputs -------------------------------------------------------------
    def auth_public(self) -> dict:
        """Auth state safe to show anyone sitting at the screen."""
        snap = self.auth.snapshot()
        state = self.effective_state()
        trust, x = self._assess()
        reason = self.live.reason if state in ("liveness", "spoof") else snap.reason
        if state == "approved" and trust.level == 0:
            # the fusion score vetoed the match
            state, reason = "scanning", REASONS[trust.blockers.get(1, "low_confidence")]
        owner = trust.level >= 1
        fs = self._face_session
        if fs is not None and state == "approved":
            reason = "Face verified at launch · camera off · voice from here on"
        if not self.face_on:
            reason = "Voice sign-in · face check off" if state == "approved" else "Finish setup to sign in"
        out = {"state": state, "reason": reason, "faces": snap.faces, "level": trust.level,
               "face_once": fs is not None}
        if owner:
            out["trust"] = {
                "level": trust.level,
                "name": LEVEL_NAMES[trust.level],
                "prob": None if trust.prob is None else round(trust.prob, 3),
                "presence": None if trust.presence is None else round(trust.presence, 3),
                "blockers": {str(k): REASONS.get(v, v) for k, v in trust.blockers.items()},
                "l3_ready": trust.l3_ready,
                "voice_window_s": trust.voice_window_s,
                "features": {k: round(float(v), 3) for k, v in zip(FEATURES, x)},
                "model": self.fusion_source,
            }
        if owner:
            # values are only revealed to the verified owner
            csim = fs["confidence_sim"] if fs is not None else snap.confidence_sim
            out["face_confidence"] = (
                None if csim is None else round(confidence(csim, self.s.face_threshold), 3)
            )
            out["bystander"] = snap.bystander
        if self.s.liveness_enabled and self.face_on:
            out["liveness"] = self.live.public(self.clock(), owner)
        else:
            out["liveness"] = {"state": "disabled", "reason": "Liveness checks are turned off"}
        if self.voice is not None:
            out["voice"] = self.voice.public(owner_verified=owner)
        return out

    def _push_state(self) -> None:
        self._last_state_push = time.monotonic()
        self.bus.publish({"type": "auth", **self.auth_public()})

    def _push_preview(self, frame: np.ndarray, boxes: list[list[float]]) -> None:
        import cv2  # OpenCV is only needed by the face sign-in: loaded when it runs
        h, w = frame.shape[:2]
        scale = self.s.preview_width / w
        small = cv2.resize(frame, (self.s.preview_width, int(h * scale)))
        ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 60])
        if not ok:
            return
        self.bus.publish(
            {
                "type": "preview",
                "jpeg": base64.b64encode(jpg.tobytes()).decode(),
                "boxes": boxes,
            }
        )

    def _liveness_status(self) -> str:
        if not self.s.liveness_enabled:
            return "disabled"
        passive = getattr(self.engine, "liveness", None)
        if passive is None or not passive.ready:
            # challenges still run; only the texture model is missing
            return "challenges only — " + (getattr(passive, "error", None) or "passive model not loaded")
        return "ready"

    def _models(self) -> dict:
        return {
            "face": ("ready" if self.engine.ready else (self.engine.error or "not loaded")) if self.face_on else "disabled",
            "voice": self.voice.model_status() if self.voice else "disabled",
            "liveness": self._liveness_status() if self.face_on else "disabled",
            "llm": self.brain.status() if self.brain else "disabled",
            "tools": "ready" if self.tools else "disabled",
            "memory": (
                ("ready" if self.memory.semantic else f"word match only — {self.memory.embed_error}")
                if self.memory else "disabled"
            ),
            **(self.speech.status() if self.speech else {"stt": "disabled", "tts": "disabled"}),
        }

    def _files_denied(self) -> list[str]:
        f = getattr(self.tools, "files", None) if self.tools else None
        try:
            return [p.name for p in f.denied()] if f is not None else []
        except Exception:
            return []

    def network_info(self) -> dict:
        stats = self.perf.stats or {}
        ext = stats.get("external", [])
        attempts = self.guard.recent()
        return {"offline": self.prefs.get("privacy.offline"), "external": ext, "attempts": attempts[:20],
                "blocked": sum(1 for a in attempts if a["blocked"])}

    def status(self) -> dict:
        allowed, why = self.face_enroll_allowed() if self.setup_complete else (True, "")
        return {
            "setup_complete": self.setup_complete,
            "version": __version__,
            # required models not on disk: the UI shows the download screen first
            "models_needed": bool(self.downloader.needed()) if self.downloader is not None else False,
            "owner_name": self.owner_name,
            "assistant_name": self.assistant_name,
            "face_enrolled": self.face_enrolled,
            "face_auth": self.face_on,
            "voice_enrolled": self.voice_enrolled,
            "mode": self.mode,
            "voice_mode": self.voice.mode if self.voice else "unavailable",
            "camera": ({"status": self.camera.status, "error": self.camera.error} if self.face_on
                       else {"status": "off", "error": None}),
            "mic": self.mic_info(),
            "models": (models := self._models()),
            "issues": self.issues(models),
            "perf": {"mode": self._mode, "pref": self.prefs.get("perf.mode"), "on_battery": self.perf.on_battery,
                     "battery_pct": self.perf.battery_pct,
                     **{k: v for k, v in (self.perf.stats or {}).items() if k in ("cpu", "backend_mb", "ollama_mb")}},
            "network": {"offline": self.prefs.get("privacy.offline"),
                        "external": sum(c["count"] for c in (self.perf.stats or {}).get("external", [])),
                        "blocked": sum(1 for a in self.guard.recent() if a["blocked"])},
            # face profile missing or a re-scan authorised: the UI shows the scan screen
            "face_reenroll": (
                {"allowed": allowed, "reason": why, "locked_out": self.locked_out(),
                 "kind": self._reenroll[0] if self._reenroll and self.clock() < self._reenroll[1] else None}
                if self.face_on and self.setup_complete and (not self.face_enrolled or self.mode == "enrolling"
                                            or (self._reenroll and self.clock() < self._reenroll[1]))
                else None
            ),
            "voice_gender": self.speech.voice_gender() if self.speech else self.db.get("voice_gender", "female"),
            "listening": bool(self.speech and self.speech.listening),
            "llm_model": self.brain.model if self.brain else None,
            # alarm state is shown to anyone at the screen, like a phone alarm
            "ringing": [
                {"id": a.id, "kind": a.kind, "label": a.label, "due": a.due.isoformat(timespec="minutes")}
                for a in (self.alarms.ringing() if self.alarms else [])
            ],
            "auth": (auth := self.auth_public()),
            "suggestions": (
                [{"id": x.id, "text": x.text} for x in self.memory.suggestions()]
                if self.memory is not None and auth.get("level", 0) >= 1 else []
            ),
            # what is about to be deleted is shown to the verified owner only
            "pending": (
                {"id": p.id, "tool": p.plan.tool, "text": p.say, "expires_s": round(p.expires - self.clock(), 1),
                 "detail": p.plan.script.script if p.plan.script is not None else ""}
                if (p := self.pending()) is not None and auth.get("level", 0) >= 1
                else None
            ),
        }
