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
from dataclasses import dataclass
from typing import Callable

import cv2
import numpy as np

from .auth.face.continuous import ContinuousFaceAuth, FrameResult
from .auth.face.engine import FaceEngine
from .auth.face.enrollment import EnrollmentSession
from .auth.face.types import FaceObservation
from .auth.fusion.features import FEATURES, Evidence, vector, without_voice
from .auth.fusion.model import FusionModel, load_model
from .auth.fusion.samples import SampleStore
from .auth.fusion.train import train as train_fusion
from .auth.levels import LEVEL_NAMES, REASONS, LevelConfig, Trust, assess
from .auth.liveness.gate import LiveObs, LivenessConfig, LivenessGate
from .auth.liveness.replay import FrozenFeedDetector
from .auth.matching import TemplateMatcher, confidence
from .camera.capture import Camera
from .config import Settings
from .database.db import Database
from .events import EventBus
from .security.template_store import TemplateStore
from .brain import Brain
from .tools.runner import LEVELS, Plan, ToolResult, ToolRunner
from .tools.scheduler import AlarmScheduler
from .tools.store import Alarm
from .speech_service import SpeechService
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

# spoken when an action needs a higher level than the evidence allows
BLOCKED_SAY = {
    "voice_needed": ("To do that I need to hear your voice. Please say it out loud.",
                     "यह करने के लिए मुझे आपकी आवाज़ सुननी होगी। कृपया बोलकर कहिए।"),
    "voice_needed_voice": ("I couldn't confirm your voice. Please say that again.",
                           "आपकी आवाज़ पक्की नहीं हो पाई। कृपया दोबारा कहिए।"),
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
    """A deletion waiting for the owner's confirmation."""

    id: str
    plan: Plan
    lang: str
    say: str
    expires: float  # monotonic


class AssistantService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        store: TemplateStore,
        bus: EventBus,
        engine: FaceEngine,
        camera: Camera,
        voice_factory: "Callable[[AssistantService], VoiceService] | None" = None,
        speech_factory: "Callable[[AssistantService], SpeechService] | None" = None,
        brain_factory: "Callable[[AssistantService], Brain] | None" = None,
        tools_factory: "Callable[[AssistantService], tuple[ToolRunner, AlarmScheduler]] | None" = None,
    ):
        self.s = settings
        self.clock: Callable[[], float] = time.monotonic  # same clock as the face loop's ``now``
        self.db = db
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
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_state_push = 0.0
        self._lock = threading.Lock()
        self.brain: Brain | None = brain_factory(self) if brain_factory else None
        self._command_lock = threading.Lock()
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
        if self.speech is not None:
            self.speech.confirm_pending = lambda: self.pending() is not None
            self.speech.on_confirm = self.confirm_spoken
            self.speech.on_voice_mismatch = lambda: self.record_sample(0, "voice_mismatch")

    def owner_verified(self) -> bool:
        """Level 1 or higher: the live owner is at the screen."""
        return self.trust().level >= 1

    # -- auth levels -----------------------------------------------------------
    def evidence(self, now: float | None = None) -> Evidence:
        now = self.clock() if now is None else now
        a = self.auth
        ev = Evidence(
            face_state=self.effective_state(),
            liveness=self.live.state if self.s.liveness_enabled else "disabled",
            face_sim=a.smoothed,
            face_age_s=None if a.last_verified_t is None else now - a.last_verified_t,
            face_quality=self._face_quality,
            live_score=self.live.live_score if self.s.liveness_enabled else None,
            live_age_s=None if self.live.passed_t is None else now - self.live.passed_t,
            voice_enrolled=self.voice_enrolled,
        )
        snap = a.snapshot()
        ev.others = max(0, snap.faces - 1)
        ev.bystander = snap.bystander
        v = self.voice
        if v is not None and v.mode == "verifying":
            if v.auth.last is not None:
                ev.voice_sim = v.auth.last.similarity
                ev.voice_age_s = now - v.auth.last.t
            if v.auth.last_verified is not None:
                ev.voice_verified_age_s = now - v.auth.last_verified.t
            ev.voice_rejected_since = v.auth.rejected_since_verified
        return ev

    def _assess(self, now: float | None = None) -> tuple[Trust, "np.ndarray"]:
        ev = self.evidence(now)
        x = vector(ev, self.levels.voice_window_s)
        if self.fusion is None:
            return assess(ev, None, self.levels), x
        return assess(ev, self.fusion.prob(x), self.levels, self.fusion.prob(without_voice(x))), x

    def trust(self, now: float | None = None) -> Trust:
        return self._assess(now)[0]

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
    def start(self) -> None:
        self.bus.log("Core systems online")
        if self.engine.load():
            self.bus.log("Face recognition model loaded")
        else:
            self.bus.log(self.engine.error or "Face model unavailable", "error")
        self.camera.start()
        if self.speech is not None:
            self.speech.start()
        if self.voice is not None:
            self.voice.start()
        if self.alarms is not None:
            self.alarms.start()
            for a in self.alarms.missed:
                self.bus.log(f"Missed {a.kind} at {a.due:%H:%M} (the app was closed)", "warn")
        if self.brain is not None:
            if self.brain.start():
                self.bus.log(f"Local language model ready ({self.brain.model})")
            else:
                self.bus.log(f"Language model unavailable: {self.brain.status()}", "error")
        if self.setup_complete and self.face_enrolled:
            self.begin_verification()
        self._thread = threading.Thread(target=self._loop, name="face-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
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

    def begin_verification(self) -> bool:
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
            period = 1.0 / max(fps, 1.0)
            t0 = time.monotonic()
            try:
                self._tick(t0)
            except Exception:
                log.exception("face loop tick failed")
            self._stop.wait(max(0.0, period - (time.monotonic() - t0)))

    def _tick(self, now: float) -> None:
        frame = self.camera.latest()
        if frame is None or not self.engine.ready or self.mode == "idle":
            if self.mode == "idle" and frame is not None and self.bus.has_subscribers:
                self._push_preview(frame, [])
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
        if self.bus.has_subscribers:
            self._push_preview(frame, faces)
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
            template = session.template()
            self.store.save(FACE, template)
            health = TemplateMatcher(template, self.s.face_top_k).self_consistency()
            with self._lock:
                self.enrollment = None
                self.mode = "idle"
            log.info("face template saved: %d samples, self-consistency %.3f", len(template), health)
            self.bus.log(f"Face profile saved ({len(template)} encrypted samples)")
            self.bus.publish({"type": "enroll_complete"})

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
    def command(self, text: str, lang: str = "en", source: str = "voice") -> dict:
        """Run one owner command through the brain and answer (shown + spoken).

        Callers have already checked level 1 (the live owner is at the
        screen). Each proposed action is checked against the level it needs
        when it runs; deletions become a pending confirmation.
        """
        text = " ".join(text.split())[:500]
        self.bus.publish({"type": "heard", "text": text, "lang": lang, "source": source})
        if self.brain is None:
            reply = "My language model is turned off."
            result = {"reply": reply, "language": lang, "actions": [], "ok": False}
        else:
            self.bus.publish({"type": "thinking", "active": True})
            with self._command_lock:  # one at a time; the model is the bottleneck
                r = self.brain.respond(text, lang)
            self.bus.publish({"type": "thinking", "active": False})
            actions = [{"tool": a.tool, "args": a.args, "summary": a.summary} for a in r.actions]
            reply = r.reply
            if r.actions and self.tools is not None:
                results = self._run_tools(r.actions, r.language, source)
                for info, res in zip(actions, results):
                    info.update(ok=res.ok, result=res.say, data=res.data)
                reply = " ".join(res.say for res in results)
            result = {"reply": reply, "language": r.language, "actions": actions, "ok": r.ok, "latency_s": round(r.latency_s, 2)}
            kinds = ", ".join(a.tool for a in r.actions) or "conversation"
            self.bus.log(f"Command understood ({kinds}) in {r.latency_s:.1f} s" if r.ok else "Language model unavailable", "info" if r.ok else "error")
        self.bus.publish({"type": "reply", "text": result["reply"], "actions": result["actions"]})
        self.bus.publish({"type": "say", "text": result["reply"]})
        return result

    def _blocked_say(self, code: str, lang: str, source: str) -> str:
        if code == "voice_needed" and source == "voice":
            code = "voice_needed_voice"  # they did speak; the match was just unclear
        en, hi = BLOCKED_SAY.get(code, (REASONS.get(code, "Not allowed right now."),) * 2)
        return hi if lang != "en" else en

    def _run_tools(self, actions, lang: str, source: str = "voice") -> list[ToolResult]:
        hi = lang != "en"
        results: list[ToolResult] = []
        did_level2 = False
        for a in actions:
            need = LEVELS.get(a.tool, 3)
            trust = self.trust()  # re-checked per action: the owner may have left while the model was thinking
            if trust.level == 0:
                results.append(ToolResult(a.tool, False, "रुक गया: आप अब सत्यापित नहीं हैं।" if hi
                                          else "Stopped: you're no longer verified."))
                self.db.add_security_event("tool_blocked", f"{a.tool} blocked: owner no longer verified", blocked=True)
                break
            if need >= 2 and trust.level < 2:
                code = trust.blockers.get(2, "low_confidence")
                results.append(ToolResult(a.tool, False, self._blocked_say(code, lang, source), {"blocked": code}))
                self.db.add_security_event(
                    "tool_blocked", f"{a.tool} needs level {need}, have {trust.level}: {REASONS.get(code, code)}", blocked=True
                )
                self.bus.log(f"Tool {a.tool} blocked — {REASONS.get(code, code)}", "warn")
                continue
            if need == 3:
                results.append(self._request_confirmation(a, lang, trust))
                continue
            res = self.tools.run(a, lang)
            self.bus.log(f"Tool {a.tool}: {'done' if res.ok else 'failed'}", "ok" if res.ok else "warn")
            results.append(res)
            did_level2 |= res.ok and need >= 2
        if did_level2:
            self.record_sample(1, "owner_command")
        return results

    # -- confirmations (level 3) ------------------------------------------------------
    def pending(self) -> Pending | None:
        p = self._pending
        return p if p is not None and self.clock() < p.expires else None

    def _finish_pending(self, outcome: str) -> Pending | None:
        with self._pending_lock:
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
            self.bus.log("Deletion not confirmed in time — cancelled", "warn")
        elif self.trust(now).level == 0:
            self._finish_pending("cancelled")
            self.bus.log("Pending deletion cancelled — owner no longer verified", "warn")

    def _request_confirmation(self, action, lang: str, trust: Trust) -> ToolResult:
        hi = lang != "en"
        if self.pending() is not None:
            return ToolResult(action.tool, False, "एक बार में एक ही चीज़ हटा सकती हूँ।" if hi else "One deletion at a time, please.")
        plan = self.tools.plan(action, lang)
        if isinstance(plan, ToolResult):
            return plan
        ttl = self.s.confirm_ttl_s
        say = f"{plan.what} हटा दूँ? “हाँ, कर दो” बोलिए या Confirm दबाइए।" if hi else (
            f"Delete {plan.what}? Say “yes, go ahead” or click Confirm.")
        if trust.needs_fresh_liveness:
            self._demand_liveness = "Fresh liveness check before a deletion"
            ttl += 20.0  # time for the challenge
            say = self._blocked_say("fresh_liveness", lang, "voice") + " " + say
        pid = secrets.token_hex(4)
        with self._pending_lock:
            self._pending = Pending(pid, plan, lang, say, self.clock() + ttl)
        self.bus.publish({"type": "confirm", "id": pid, "tool": plan.tool, "text": say, "expires_s": ttl})
        self.bus.log(f"Waiting for confirmation: {plan.tool}")
        return ToolResult(action.tool, True, say, {"pending": pid})

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
            self._finish_pending("cancelled")
            self.bus.log("Deletion cancelled by the owner")
            return self._answer("ठीक है, नहीं हटाया।" if hi else "Okay, I won't delete it.", ok=True)
        if source == "voice" and verdict != "verified":
            # a short "yes" carries too little voice to identify the speaker
            return self._answer(
                "आपकी आवाज़ पक्की नहीं हो पाई। “हाँ, कर दो” साफ़ बोलिए या Confirm दबाइए।" if hi
                else "I couldn't verify your voice from that. Say “yes, go ahead” clearly, or click Confirm.",
                ok=False,
            )
        trust = self.trust()
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
        self._finish_pending("done")
        res = self.tools.execute(p.plan)
        self.db.add_security_event("sensitive_action", f"{p.plan.tool} confirmed by {'voice' if source == 'voice' else 'click'}")
        self.bus.log(f"Tool {p.plan.tool}: {'done' if res.ok else 'failed'} (confirmed)", "ok" if res.ok else "warn")
        self.record_sample(1, "owner_confirm")
        action = {"tool": p.plan.tool, "args": {}, "summary": p.plan.what, "ok": res.ok, "result": res.say, "data": res.data}
        return self._answer(res.say, ok=res.ok, actions=[action])

    def _answer(self, text: str, ok: bool, actions: list | None = None) -> dict:
        self.bus.publish({"type": "reply", "text": text, "actions": actions or []})
        self.bus.publish({"type": "say", "text": text})
        return {"ok": ok, "reply": text}

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
        out = {"state": state, "reason": reason, "faces": snap.faces, "level": trust.level}
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
            out["face_confidence"] = (
                None
                if snap.confidence_sim is None
                else round(confidence(snap.confidence_sim, self.s.face_threshold), 3)
            )
            out["bystander"] = snap.bystander
        if self.s.liveness_enabled:
            out["liveness"] = self.live.public(self.clock(), owner)
        else:
            out["liveness"] = {"state": "disabled", "reason": "Liveness checks are turned off"}
        if self.voice is not None:
            out["voice"] = self.voice.public(owner_verified=owner)
        return out

    def _push_state(self) -> None:
        self._last_state_push = time.monotonic()
        self.bus.publish({"type": "auth", **self.auth_public()})

    def _push_preview(self, frame: np.ndarray, faces: list[FaceObservation]) -> None:
        h, w = frame.shape[:2]
        scale = self.s.preview_width / w
        small = cv2.resize(frame, (self.s.preview_width, int(h * scale)))
        ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 60])
        if not ok:
            return
        boxes = [[float(v) for v in (f.bbox / np.array([w, h, w, h]))] for f in faces]
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

    def status(self) -> dict:
        return {
            "setup_complete": self.setup_complete,
            "owner_name": self.owner_name,
            "assistant_name": self.assistant_name,
            "face_enrolled": self.face_enrolled,
            "voice_enrolled": self.voice_enrolled,
            "mode": self.mode,
            "voice_mode": self.voice.mode if self.voice else "unavailable",
            "camera": {"status": self.camera.status, "error": self.camera.error},
            "mic": (
                {"status": self.voice.mic.status, "error": self.voice.mic.error, "device": self.voice.mic.device_name}
                if self.voice
                else {"status": "off", "error": "voice pipeline disabled", "device": None}
            ),
            "models": {
                "face": "ready" if self.engine.ready else (self.engine.error or "not loaded"),
                "voice": self.voice.model_status() if self.voice else "disabled",
                "liveness": self._liveness_status(),
                "llm": self.brain.status() if self.brain else "disabled",
                "tools": "ready" if self.tools else "disabled",
                **(self.speech.status() if self.speech else {"stt": "disabled", "tts": "disabled"}),
            },
            "voice_gender": self.speech.voice_gender() if self.speech else self.db.get("voice_gender", "female"),
            "listening": bool(self.speech and self.speech.listening),
            "llm_model": self.brain.model if self.brain else None,
            # alarm state is shown to anyone at the screen, like a phone alarm
            "ringing": [
                {"id": a.id, "kind": a.kind, "label": a.label, "due": a.due.isoformat(timespec="minutes")}
                for a in (self.alarms.ringing() if self.alarms else [])
            ],
            "auth": (auth := self.auth_public()),
            # what is about to be deleted is shown to the verified owner only
            "pending": (
                {"id": p.id, "tool": p.plan.tool, "text": p.say, "expires_s": round(p.expires - self.clock(), 1)}
                if (p := self.pending()) is not None and auth.get("level", 0) >= 1
                else None
            ),
        }
