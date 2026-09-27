import { useEffect, useState } from "react";
import ActivityFeed from "../components/ActivityFeed";
import CameraPreview from "../components/CameraPreview";
import Orb, { OrbMode } from "../components/Orb";
import SideNav, { View } from "../components/SideNav";
import VoiceEnroll from "../components/VoiceEnroll";
import { ApiError, api } from "../lib/backend";
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

function Core() {
  const { connected, status, auth, speech } = useStore();
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
      title = justChanged ? "AUTHENTICATION APPROVED" : `${name} ONLINE`;
      sub = justChanged ? `Welcome back, ${status?.owner_name}.` : "Voice commands arrive in phase 4";
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
    <div className={`core state-${state}`}>
      <Orb mode={mode} listen={state !== "offline"} className="core-orb" />
      <div className="core-text">
        <h1>{title}</h1>
        <p>{sub}</p>
        <FactorBadges />
        <p className={`speech ${showSpeech ? "show" : ""}`}>{speech?.text}</p>
      </div>
    </div>
  );
}

const VOICE_LABEL: Record<string, [string, string]> = {
  verified: ["VERIFIED", "tone-ok"],
  uncertain: ["UNCLEAR", "tone-warn"],
  rejected: ["NOT RECOGNISED", "tone-alert"],
  idle: ["WAITING FOR SPEECH", "tone-off"],
};

function FactorBadges() {
  const { auth, status, speaking } = useStore();
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
      <span className="tone-off">
        <i className="dot" /> LIVENESS · P3
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
          {row("Status", approved ? "VERIFIED" : (auth?.state ?? "—").toUpperCase(), approved ? "tone-ok" : "tone-warn")}
          {/* confidence values are revealed only to the verified owner */}
          {row("Confidence", approved && auth?.face_confidence != null ? `${Math.round(auth.face_confidence * 100)}%` : "hidden")}
          {row("Faces in view", String(auth?.faces ?? 0))}
          {approved && auth?.bystander && row("Bystander", "UNKNOWN PERSON PRESENT", "tone-alert")}
          {row("Model", status?.models.face === "ready" ? "ArcFace R50 · local" : (status?.models.face ?? "—"))}
        </div>
        <div className="card">
          <div className="panel-title">VOICE</div>
          {row("Status", vLabel[0], vLabel[1])}
          {row("Confidence", approved && voice?.confidence != null ? `${Math.round(voice.confidence * 100)}%` : "hidden")}
          {row("Last heard", approved && voice?.seconds_ago != null ? `${Math.round(voice.seconds_ago)} s ago` : "—")}
          {row("Microphone", status?.mic.status === "active" ? (status.mic.device ?? "active") : (status?.mic.status ?? "—").toUpperCase(), status?.mic.status === "active" ? "" : "tone-alert")}
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
          {row("Status", "NOT YET IMPLEMENTED", "tone-off")}
          <p className="muted small">
            Until phase 3, a photo of the owner may pass face verification. Liveness checks close that gap.
          </p>
        </div>
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
      </main>
      <ActivityFeed />
    </div>
  );
}
