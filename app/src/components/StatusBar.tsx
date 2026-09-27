import { useEffect, useState } from "react";
import { useStore } from "../lib/store";

type Tone = "ok" | "warn" | "alert" | "off";

function Indicator({ label, value, tone }: { label: string; value: string; tone: Tone }) {
  return (
    <div className="indicator">
      <span className="indicator-label">{label}</span>
      <span className={`indicator-value tone-${tone}`}>
        <i className="dot" />
        {value}
      </span>
    </div>
  );
}

export default function StatusBar() {
  const { connected, status, auth, speaking } = useStore();
  const mic = status?.mic.status ?? "off";
  const [clock, setClock] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setClock(new Date()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const cam = status?.camera.status ?? "off";
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
        <Indicator label="LOCAL AI" value="PHASE 5" tone="off" />
        <Indicator
          label="CAMERA"
          value={cam === "active" ? "ACTIVE" : cam === "error" ? "BLOCKED" : cam.toUpperCase()}
          tone={cam === "active" ? "ok" : cam === "error" ? "alert" : "warn"}
        />
        <Indicator
          label="MIC"
          value={mic === "active" ? (speaking ? "HEARING" : "ACTIVE") : mic === "error" ? "BLOCKED" : mic.toUpperCase()}
          tone={mic === "active" ? "ok" : mic === "error" ? "alert" : "warn"}
        />
        <Indicator label="SECURITY" value={security[0]} tone={security[1]} />
      </div>
      <div className="clock" data-tauri-drag-region>
        {clock.toLocaleTimeString([], { hour12: false })}
      </div>
    </header>
  );
}
