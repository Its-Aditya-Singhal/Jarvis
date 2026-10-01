import { useEffect, useState } from "react";
import { useStore } from "../lib/store";

type Tone = "ok" | "warn" | "alert" | "off";

function Indicator({ label, value, tone, title }: { label: string; value: string; tone: Tone; title?: string }) {
  return (
    <div className="indicator" title={title}>
      <span className="indicator-label">{label}</span>
      <span className={`indicator-value tone-${tone}`}>
        <i className="dot" />
        {value}
      </span>
    </div>
  );
}

export default function StatusBar() {
  const { connected, status, auth, speaking, assistantSpeaking, listeningUntil, thinking } = useStore();
  const llm = status?.models.llm;
  const mic = status?.mic.status ?? "off";
  const [clock, setClock] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setClock(new Date()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const cam = status?.camera.status ?? "off";
  const net = status?.network;
  const perf = status?.perf;
  const security: [string, Tone] =
    auth?.state === "denied"
      ? ["INTRUDER", "alert"]
      : auth?.state === "spoof"
        ? ["SPOOF", "alert"]
        : auth?.state === "liveness"
          ? ["LIVENESS", "warn"]
          : auth?.state === "approved"
        ? ["PROTECTED", "ok"]
        : auth?.state === "absent"
          ? ["LOCKED", "warn"]
          : status?.face_enrolled
            ? ["VERIFYING", "warn"]
            : ["NO PROFILE", "off"];

  return (
    <header className="statusbar" data-tauri-drag-region>
      <div className="brand" data-tauri-drag-region>
        {(status?.assistant_name ?? "JARVIS").toUpperCase()}
      </div>
      <div className="indicators" data-tauri-drag-region>
        <Indicator label="CORE" value={connected ? "ONLINE" : "OFFLINE"} tone={connected ? "ok" : "alert"} />
        <Indicator
          label="AI"
          value={thinking ? "THINKING" : llm === "ready" ? "ONLINE" : llm ? "OFFLINE" : "—"}
          tone={llm === "ready" ? "ok" : "alert"}
        />
        {status?.face_auth !== false && (
        <Indicator
          label="CAMERA"
          value={cam === "active" ? "ACTIVE" : cam === "error" ? "BLOCKED" : cam.toUpperCase()}
          tone={cam === "active" ? "ok" : cam === "error" ? "alert" : "warn"}
        />
        )}
        <Indicator
          label="MIC"
          value={
            mic === "active"
              ? listeningUntil > Date.now()
                ? "LISTENING"
                : speaking
                  ? "HEARING"
                  : "ACTIVE"
              : mic === "error"
                ? "BLOCKED"
                : mic.toUpperCase()
          }
          tone={mic === "active" ? "ok" : mic === "error" ? "alert" : "warn"}
        />
        <Indicator
          label="SPEECH"
          value={assistantSpeaking ? "SPEAKING" : status?.models.stt === "ready" && status?.models.tts === "ready" ? "READY" : "LOADING"}
          tone={status?.models.stt === "ready" && status?.models.tts === "ready" ? "ok" : "warn"}
        />
        <Indicator label="SECURITY" value={security[0]} tone={security[1]} />
        <Indicator
          label="NETWORK"
          value={!net ? "—" : net.external > 0 ? `${net.external} OUT` : net.offline ? "OFFLINE ✓" : "ALLOWED"}
          tone={!net ? "off" : net.external > 0 ? "alert" : net.offline ? "ok" : "warn"}
          title={
            net?.offline
              ? `Internet blocked; everything runs on this Mac${net.blocked ? ` · ${net.blocked} attempts blocked` : ""}`
              : "Internet access allowed (Privacy → Network)"
          }
        />
        <Indicator
          label="MODE"
          value={
            perf
              ? `${perf.mode.toUpperCase()}${perf.on_battery ? ` · 🔋${perf.battery_pct ?? ""}%` : ""}`
              : "—"
          }
          tone={perf ? "ok" : "off"}
          title={
            perf
              ? `${perf.pref === "auto" ? "Auto: Fast on battery, Balanced plugged in" : "Chosen in Settings"} · CPU ${perf.cpu ?? "—"}% · backend ${perf.backend_mb ?? "—"} MB · models ${perf.ollama_mb ?? "—"} MB`
              : undefined
          }
        />
        <Indicator
          label="LEVEL"
          value={`L${auth?.level ?? 0} ${["LOCKED", "READ", "ACT", "CONFIRM"][auth?.level ?? 0] ?? ""}`}
          tone={(auth?.level ?? 0) >= 2 ? "ok" : (auth?.level ?? 0) === 1 ? "warn" : "off"}
        />
      </div>
      <div className="clock" data-tauri-drag-region>
        {clock.toLocaleTimeString([], { hour12: false })}
      </div>
    </header>
  );
}
