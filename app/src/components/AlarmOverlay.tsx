import { useState } from "react";
import { ApiError, post } from "../lib/backend";
import { useStore } from "../lib/store";

/** Full-screen alarm/timer alert. Like a phone alarm, anyone may silence it. */
export default function AlarmOverlay() {
  const { ringing } = useStore();
  const [error, setError] = useState<string | null>(null);
  if (!ringing.length) return null;
  const a = ringing[0];
  const time = new Date(a.due).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const send = (path: string, body?: unknown) => {
    setError(null);
    post(path, body).catch((e) =>
      setError(e instanceof ApiError ? e.message : "Can't reach the assistant — say “stop”, or quit the app."),
    );
  };
  return (
    <div className="alarm-overlay" role="alertdialog" aria-label={a.kind === "timer" ? "Timer finished" : "Alarm"}>
      <div className="alarm-card">
        <div className="alarm-rings">
          <i />
          <i />
          <i />
        </div>
        <div className="alarm-kind">{a.kind === "timer" ? "TIMER FINISHED" : "ALARM"}</div>
        <div className="alarm-time">{time}</div>
        {a.label && <div className="alarm-label">{a.label}</div>}
        <div className="alarm-actions">
          <button className="btn primary" onClick={() => send("/api/alarms/dismiss")}>
            DISMISS
          </button>
          {a.kind === "alarm" && (
            <button className="btn ghost" onClick={() => send("/api/alarms/snooze", { minutes: 5 })}>
              SNOOZE 5 MIN
            </button>
          )}
        </div>
        {error && <p className="error small">{error}</p>}
        <p className="muted small">Or just say “stop”.</p>
      </div>
    </div>
  );
}
