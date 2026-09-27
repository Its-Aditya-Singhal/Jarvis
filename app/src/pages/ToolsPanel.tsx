import { useEffect, useState } from "react";
import { ApiError, api, post } from "../lib/backend";
import { useStore } from "../lib/store";

interface ToolsState {
  alarms: { id: number; kind: "alarm" | "timer"; due: string; label: string; status: string }[];
  events: { id: number; title: string; start: string; end: string; apple: boolean }[];
  notes: { id: number; text: string; created: string; apple: boolean }[];
}

const fmtDay = (iso: string) =>
  new Date(iso).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
const fmtTime = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

function Countdown({ due }: { due: string }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  const left = Math.max(0, Math.round((new Date(due).getTime() - now) / 1000));
  const h = Math.floor(left / 3600);
  const m = Math.floor((left % 3600) / 60);
  const s = left % 60;
  return <>{h ? `${h}:${String(m).padStart(2, "0")}` : m}:{String(s).padStart(2, "0")}</>;
}

/** Alarms, timers, upcoming events and notes (decrypted for the verified owner only). */
export default function ToolsPanel() {
  const { auth, toolsVersion } = useStore();
  const approved = auth?.state === "approved";
  const [data, setData] = useState<ToolsState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!approved) {
      setData(null);
      return;
    }
    let live = true;
    api<ToolsState>("/api/tools")
      .then((d) => live && (setData(d), setError(null)))
      .catch((e) => live && setError(e instanceof ApiError ? e.message : "unavailable"));
    return () => {
      live = false;
    };
  }, [approved, toolsVersion]);

  if (!approved) {
    return (
      <div className="view">
        <h2 className="view-title">TOOLS</h2>
        <div className="card locked-card">Owner verification required to view alarms, calendar and notes.</div>
      </div>
    );
  }
  return (
    <div className="view">
      <h2 className="view-title">TOOLS</h2>
      {error && <div className="card error">{error}</div>}
      <div className="cards">
        <div className="card">
          <div className="panel-title">ALARMS &amp; TIMERS</div>
          {data?.alarms.length === 0 && <p className="muted small">None set. Try “set an alarm for 7 tomorrow”.</p>}
          {data?.alarms.map((a) => (
            <div className="kv item" key={a.id}>
              <span>
                <b className="tag">{a.kind === "timer" ? "TIMER" : "ALARM"}</b>
                {a.kind === "timer" ? <Countdown due={a.due} /> : `${fmtDay(a.due)} · ${fmtTime(a.due)}`}
                {a.label && <em> — {a.label}</em>}
              </span>
              <button className="btn ghost mini" onClick={() => post(`/api/alarms/${a.id}/cancel`)}>
                CANCEL
              </button>
            </div>
          ))}
        </div>
        <div className="card">
          <div className="panel-title">CALENDAR · NEXT 14 DAYS</div>
          {data?.events.length === 0 && <p className="muted small">No events. Try “add dinner with Riya on Friday at 8”.</p>}
          {data?.events.map((e) => (
            <div className="kv item" key={e.id}>
              <span>
                {fmtDay(e.start)} · {fmtTime(e.start)} — {e.title}
              </span>
              {e.apple && <b className="tag">APPLE</b>}
            </div>
          ))}
        </div>
        <div className="card">
          <div className="panel-title">NOTES · ENCRYPTED</div>
          {data?.notes.length === 0 && <p className="muted small">No notes yet. Try “note: call the bank tomorrow”.</p>}
          {data?.notes.map((n) => (
            <div className="kv item" key={n.id}>
              <span>{n.text}</span>
              <span className="muted small">
                {fmtDay(n.created)}
                {n.apple && <b className="tag">APPLE</b>}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
