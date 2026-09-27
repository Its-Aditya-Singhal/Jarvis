import { useEffect, useState } from "react";
import ActivityFeed from "../components/ActivityFeed";
import CameraPreview from "../components/CameraPreview";
import Orb, { OrbMode } from "../components/Orb";
import SideNav, { View } from "../components/SideNav";
import VoiceEnroll from "../components/VoiceEnroll";
import ConfirmCard from "../components/ConfirmCard";
import HealthCard from "../components/HealthCard";
import TrustCard from "../components/TrustCard";
import ToolsPanel from "./ToolsPanel";
import MemoryPanel from "./MemoryPanel";
import PrivacyPanel from "./PrivacyPanel";
import SettingsPanel from "./SettingsPanel";
import SuggestionChips from "../components/SuggestionChips";
import { ApiError, Challenge, PlannedAction, api, post } from "../lib/backend";
import { useStore } from "../lib/store";

interface SecurityEvent {
  id: number;
  time: string;
  kind: string;
  detail: string;
  face_confidence: number | null;
  voice_confidence: number | null;
  blocked: boolean;
}

function useRecentChange<T>(value: T, ms: number): boolean {
  const [recent, setRecent] = useState(false);
  useEffect(() => {
    setRecent(true);
    const id = window.setTimeout(() => setRecent(false), ms);
    return () => window.clearTimeout(id);
  }, [value, ms]);
  return recent;
}

function useNow(ms: number, active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(id);
  }, [ms, active]);
  return now;
}

function Core() {
  const { connected, status, auth, speech, listeningUntil, conversation, assistantSpeaking, thinking } = useStore();
  const now = useNow(500, listeningUntil > Date.now());
  const listening = listeningUntil > now;
  const name = (status?.assistant_name ?? "JARVIS").toUpperCase();
  const state = connected ? (auth?.state ?? "no_profile") : "offline";
  const justChanged = useRecentChange(state, 3500);
  const [showSpeech, setShowSpeech] = useState(false);
  useEffect(() => {
    if (!speech) return;
    setShowSpeech(true);
    const id = window.setTimeout(() => setShowSpeech(false), 6000);
    return () => window.clearTimeout(id);
  }, [speech]);

  let mode: OrbMode = "idle";
  let title = `${name} ONLINE`;
  let sub = "Waiting for command…";
  switch (state) {
    case "offline":
      mode = "offline";
      title = "CORE OFFLINE";
      sub = "Starting local systems…";
      break;
    case "scanning":
      mode = "scanning";
      title = "VERIFYING IDENTITY…";
      sub = auth?.reason ?? "";
      break;
    case "approved":
      mode = "approved";
      if (thinking) {
        title = "THINKING…";
        sub = `Local model · ${status?.llm_model ?? ""}`;
      } else if (listening) {
        title = "LISTENING…";
        sub = `Go ahead, ${status?.owner_name}.`;
      } else if (assistantSpeaking) {
        title = `${name} SPEAKING`;
        sub = "";
      } else {
        title = justChanged ? "AUTHENTICATION APPROVED" : `${name} ONLINE`;
        sub = justChanged
          ? `Welcome back, ${status?.owner_name}.`
          : `Say “${status?.assistant_name ?? "JARVIS"}” to talk to me`;
      }
      break;
    case "liveness": {
      const live = auth?.liveness;
      if (live?.state === "cooldown") {
        mode = "locked";
        title = "LIVENESS CHECK FAILED";
        sub = `${live.reason} · retrying in ${Math.ceil(live.cooldown_s ?? 0)} s`;
      } else {
        mode = "challenge";
        title = "LIVENESS CHECK";
        sub = live?.reason ?? "Prove you're live";
      }
      break;
    }
    case "spoof":
      mode = "denied";
      title = "SPOOF DETECTED";
      sub = `${auth?.reason ?? ""} — photos, screens and replayed video are not accepted.`;
      break;
    case "denied":
      mode = "denied";
      title = "AUTHENTICATION DENIED";
      sub = "You are not my boss.";
      break;
    case "absent":
      mode = "locked";
      title = "SESSION LOCKED";
      sub = "Look at the camera to verify";
      break;
  }

  return (
    <div className={`core state-${state} ${mode === "locked" ? "cooling" : ""}`}>
      <Orb mode={mode} listen={state !== "offline"} className="core-orb" />
      <div className="core-text">
        <h1>{title}</h1>
        <p>{sub}</p>
        {state === "liveness" && auth?.liveness?.challenge && <ChallengeCard c={auth.liveness.challenge} />}
        <FactorBadges />
        {/* replies already appear in the conversation strip */}
        {!(state === "approved" && conversation.some((t) => t.who === "assistant" && t.text === speech?.text)) && (
          <p className={`speech ${showSpeech || assistantSpeaking ? "show" : ""}`}>{speech?.text}</p>
        )}
        {state === "approved" && <ConfirmCard />}
        {state === "approved" && <SuggestionChips />}
        {state === "approved" && <Conversation turns={conversation} name={name} />}
        {state === "approved" && <CommandBox disabled={thinking} />}
        <HealthCard />
      </div>
    </div>
  );
}

function Conversation({
  turns,
  name,
}: {
  turns: { who: string; text: string; at: number; actions?: PlannedAction[] }[];
  name: string;
}) {
  const recent = turns.filter((t) => Date.now() - t.at < 120_000).slice(-2);
  if (!recent.length) return null;
  return (
    <div className="conversation">
      {recent.map((t) => {
        // search results the owner can reveal in Finder
        const files = (t.actions ?? []).flatMap((a) => a.data?.files ?? []).slice(0, 5);
        return (
          <div key={t.at} className={`turn ${t.who}`}>
            <b>{t.who === "you" ? "YOU" : name}</b>
            <span>
              {t.text}
              {t.actions && t.actions.length > 0 && (
                <span className="actions">
                  {t.actions.map((a, i) => {
                    const pending = a.data?.pending !== undefined;
                    const blocked = a.data?.blocked !== undefined;
                    const cls = a.ok === undefined || pending ? "" : a.ok ? "ok" : blocked ? "blocked" : "fail";
                    const icon = a.ok === undefined ? "○" : pending ? "?" : a.ok ? "✓" : blocked ? "⛔" : "✗";
                    return (
                      <i
                        key={i}
                        className={cls}
                        title={blocked ? `blocked: ${a.data?.blocked}` : JSON.stringify(a.args)}
                      >
                        {icon} {a.tool}
                      </i>
                    );
                  })}
                </span>
              )}
              {files.length > 0 && (
                <span className="file-hits">
                  {files.map((f) => (
                    <button
                      key={f}
                      className="file-hit"
                      title={f}
                      onClick={() => post("/api/files/reveal", { path: f })}
                    >
                      {f.split("/").pop()}
                    </button>
                  ))}
                </span>
              )}
            </span>
          </div>
        );
      })}
    </div>
  );
}

/** Typed commands for when speaking isn't convenient (owner only). */
function CommandBox({ disabled }: { disabled: boolean }) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    const t = text.trim();
    if (!t) return;
    setText("");
    setError(null);
    try {
      await post("/api/command", { text: t });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Backend unreachable");
    }
  };
  return (
    <form className="command-box" onSubmit={send}>
      <input
        className="field"
        value={text}
        maxLength={500}
        placeholder="Type a command… (English, हिंदी or Hinglish)"
        onChange={(e) => setText(e.target.value)}
        disabled={disabled}
      />
      {error && <p className="error small">{error}</p>}
    </form>
  );
}

// the preview is mirrored, so the person's left is on the left of the screen
const STEP_ICON: Record<string, string> = {
  blink: "◉ ◉",
  turn_left: "←",
  turn_right: "→",
  closer: "⤢",
};

function ChallengeCard({ c }: { c: Challenge }) {
  return (
    <div className="challenge">
      <div className="challenge-icon">{STEP_ICON[c.step] ?? "•"}</div>
      <div className="challenge-prompt">{c.prompt}</div>
      <div className="challenge-hint">{c.hint || "\u00a0"}</div>
      <div className="challenge-steps">
        {c.steps.map((st, i) => (
          <i key={i} className={i < c.step_index ? "done" : i === c.step_index ? "active" : ""} title={st} />
        ))}
        <span>{Math.ceil(c.remaining_s)} s</span>
      </div>
    </div>
  );
}

const LIVE_LABEL: Record<string, [string, string]> = {
  passed: ["LIVE PERSON CONFIRMED", "tone-ok"],
  challenge: ["CHALLENGE IN PROGRESS", "tone-warn"],
  idle: ["NOT YET PROVEN", "tone-warn"],
  cooldown: ["CHECK FAILED — WAITING", "tone-alert"],
  spoof: ["SPOOF SUSPECTED", "tone-alert"],
  disabled: ["DISABLED", "tone-off"],
};

const VOICE_LABEL: Record<string, [string, string]> = {
  verified: ["VERIFIED", "tone-ok"],
  uncertain: ["UNCLEAR", "tone-warn"],
  rejected: ["NOT RECOGNISED", "tone-alert"],
  idle: ["WAITING FOR SPEECH", "tone-off"],
};

const LEVEL_NAME = ["LOCKED", "READ", "ACT"];

function FactorBadges() {
  const { auth, status, speaking } = useStore();
  const level = auth?.level ?? 0;
  if (!status?.setup_complete) return null;
  const face = auth?.state === "approved" ? "tone-ok" : auth?.state === "denied" ? "tone-alert" : "tone-warn";
  const v = auth?.voice?.state ?? "idle";
  const voice = !status.voice_enrolled ? "tone-off" : VOICE_LABEL[v][1];
  return (
    <div className="factors">
      <span className={face}>
        <i className="dot" /> FACE
      </span>
      <span className={voice}>
        <i className="dot" /> VOICE{speaking ? " · HEARING" : ""}
      </span>
      <span className={LIVE_LABEL[auth?.liveness?.state ?? "idle"][1]}>
        <i className="dot" /> LIVENESS
        {auth?.liveness?.state === "disabled" ? " · OFF" : ""}
      </span>
      <span className={`level-pill l${level}`} title={auth?.trust?.blockers["2"] ?? ""}>
        L{level} · {LEVEL_NAME[level]}
      </span>
    </div>
  );
}

function AuthPanel() {
  const { auth, status } = useStore();
  const approved = auth?.state === "approved";
  const [enrolling, setEnrolling] = useState(false);
  // the re-enrollment dialog is for the verified owner only
  useEffect(() => {
    if (!approved) setEnrolling(false);
  }, [approved]);
  const voice = auth?.voice;
  const live = auth?.liveness;
  const vLabel = status?.voice_enrolled ? VOICE_LABEL[voice?.state ?? "idle"] : ["NOT ENROLLED", "tone-warn"];
  const row = (k: string, v: string, tone = "") => (
    <div className="kv">
      <span>{k}</span>
      <b className={tone}>{v}</b>
    </div>
  );
  return (
    <div className="view">
      <h2 className="view-title">AUTHENTICATION</h2>
      <div className="cards">
        <div className="card">
          <div className="panel-title">FACE</div>
          {row(
            "Status",
            approved ? "VERIFIED" : (auth?.state ?? "—").toUpperCase(),
            approved ? "tone-ok" : "tone-warn",
          )}
          {/* confidence values are revealed only to the verified owner */}
          {row(
            "Confidence",
            approved && auth?.face_confidence != null ? `${Math.round(auth.face_confidence * 100)}%` : "hidden",
          )}
          {row("Faces in view", String(auth?.faces ?? 0))}
          {approved && auth?.bystander && row("Bystander", "UNKNOWN PERSON PRESENT", "tone-alert")}
          {row("Model", status?.models.face === "ready" ? "ArcFace R50 · local" : (status?.models.face ?? "—"))}
        </div>
        <div className="card">
          <div className="panel-title">VOICE</div>
          {row("Status", vLabel[0], vLabel[1])}
          {row(
            "Confidence",
            approved && voice?.confidence != null ? `${Math.round(voice.confidence * 100)}%` : "hidden",
          )}
          {row("Last heard", approved && voice?.seconds_ago != null ? `${Math.round(voice.seconds_ago)} s ago` : "—")}
          {row(
            "Microphone",
            status?.mic.status === "active"
              ? (status.mic.device ?? "active")
              : (status?.mic.status ?? "—").toUpperCase(),
            status?.mic.status === "active" ? "" : "tone-alert",
          )}
          {row("Model", status?.models.voice === "ready" ? "ECAPA-TDNN · local" : (status?.models.voice ?? "—"))}
          {approved ? (
            <button className="btn ghost card-btn" onClick={() => setEnrolling(true)}>
              {status?.voice_enrolled ? "RE-ENROLL VOICE" : "ENROLL VOICE"}
            </button>
          ) : (
            <p className="muted small">Verify your face to manage the voice profile.</p>
          )}
        </div>
        <div className="card">
          <div className="panel-title">LIVENESS</div>
          {row("Status", ...(LIVE_LABEL[live?.state ?? "idle"] as [string, string]))}
          {row(
            "Anti-spoof score",
            approved && live?.live_score != null ? `${Math.round(live.live_score * 100)}%` : "hidden",
          )}
          {row("Last check", approved && live?.checked_ago != null ? `${Math.round(live.checked_ago)} s ago` : "—")}
          {row(
            "Next random check",
            approved && live?.next_check_s != null ? `within ${Math.ceil(live.next_check_s / 60)} min` : "—",
          )}
          {row(
            "Model",
            status?.models.liveness === "ready"
              ? "Anti-spoof CNN + challenges · local"
              : (status?.models.liveness ?? "—"),
          )}
          <p className="muted small">
            Randomised blink / turn / move-closer challenges at unlock and at random times, plus a texture check on
            every frame that rejects photos and screens.
          </p>
        </div>
        <TrustCard />
      </div>
      {enrolling && (
        <div className="overlay">
          <div className="overlay-card">
            <VoiceEnroll
              title={status?.voice_enrolled ? "Re-enroll your voice." : "Let's learn your voice."}
              onDone={() => setEnrolling(false)}
              onClose={() => setEnrolling(false)}
            />
          </div>
        </div>
      )}
    </div>
  );
}

const EVENT_TITLE: Record<string, string> = {
  unknown_face: "UNKNOWN USER DETECTED",
  bystander: "UNKNOWN PERSON NEAR OWNER",
  unknown_voice: "UNKNOWN VOICE DETECTED",
  spoof_suspected: "SPOOF ATTEMPT DETECTED",
  liveness_failed: "LIVENESS CHECK FAILED",
  liveness_lockout: "LIVENESS LOCKOUT",
  unauthorized_command: "COMMAND FROM UNVERIFIED USER",
  voice_mismatch_command: "COMMAND IN UNKNOWN VOICE",
  camera_frozen: "CAMERA FEED FROZEN",
  tool_blocked: "ACTION BLOCKED — LEVEL TOO LOW",
  sensitive_action: "DELETION CONFIRMED",
  fusion_retrained: "FUSION MODEL RETRAINED",
  fusion_reset: "FUSION MODEL RESET",
  privacy_action: "PRIVACY ACTION CONFIRMED",
  settings_changed: "SETTINGS CHANGED",
  face_reenrolled: "FACE PROFILE REPLACED",
  factory_reset: "FACTORY RESET",
};

function SecurityPanel() {
  const { auth } = useStore();
  const [events, setEvents] = useState<SecurityEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const approved = auth?.state === "approved";

  useEffect(() => {
    if (!approved) {
      setEvents(null);
      return;
    }
    let live = true;
    const load = () =>
      api<SecurityEvent[]>("/api/security/events")
        .then((e) => live && (setEvents(e), setError(null)))
        .catch((e) => live && setError(e instanceof ApiError ? e.message : "unavailable"));
    load();
    const id = window.setInterval(load, 4000);
    return () => {
      live = false;
      window.clearInterval(id);
    };
  }, [approved]);

  return (
    <div className="view">
      <h2 className="view-title">SECURITY LOG</h2>
      {!approved ? (
        <div className="card locked-card">Owner verification required to view the security log.</div>
      ) : error ? (
        <div className="card error">{error}</div>
      ) : (
        <div className="events">
          {events?.length === 0 && <div className="muted">No security events recorded.</div>}
          {events?.map((e) => (
            <div key={e.id} className={`event ${e.blocked ? "blocked" : ""}`}>
              <div className="event-head">
                <b>{EVENT_TITLE[e.kind] ?? e.kind.toUpperCase()}</b>
                <time>{new Date(e.time).toLocaleString()}</time>
              </div>
              <div className="event-body">
                <span>{e.detail}</span>
                {e.face_confidence != null && <span>Face match: {Math.round(e.face_confidence * 100)}%</span>}
                {e.voice_confidence != null && <span>Voice match: {Math.round(e.voice_confidence * 100)}%</span>}
                <span>{e.blocked ? "Access blocked" : "Logged"}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function Main() {
  const [view, setView] = useState<View>("system");
  return (
    <div className="main-grid">
      <div className="left-col">
        <SideNav view={view} onChange={setView} />
        <div className="panel sensor">
          <div className="panel-title">OPTICAL SENSOR</div>
          <CameraPreview />
        </div>
      </div>
      <main className="center">
        {view === "system" && <Core />}
        {view === "auth" && <AuthPanel />}
        {view === "security" && <SecurityPanel />}
        {view === "settings" && <SettingsPanel />}
        {view === "tools" && <ToolsPanel />}
        {view === "memory" && <MemoryPanel />}
        {view === "privacy" && <PrivacyPanel />}
        {/* the core view shows it inline; elsewhere it floats so a confirmation is never missed */}
        {view !== "system" && (
          <div className="confirm-float">
            <ConfirmCard />
          </div>
        )}
      </main>
      <ActivityFeed />
    </div>
  );
}
