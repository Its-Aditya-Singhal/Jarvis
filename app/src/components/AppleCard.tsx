import { useEffect, useState } from "react";
import { ApiError, api, post } from "../lib/backend";

interface AppleSettings {
  calendar_sync: boolean;
  calendar: string;
  notes_sync: boolean;
  notes_folder: string;
}

/** Optional sync with Apple Calendar / Apple Notes (owner only). */
export default function AppleCard() {
  const [cfg, setCfg] = useState<AppleSettings | null>(null);
  const [calendars, setCalendars] = useState<string[] | null>(null);
  const [msg, setMsg] = useState<{ text: string; tone: "ok" | "alert" } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<AppleSettings>("/api/settings/apple")
      .then(setCfg)
      .catch(() => setMsg({ text: "unavailable", tone: "alert" }));
  }, []);

  const loadCalendars = async () => {
    // the first call makes macOS ask for permission to control Calendar
    try {
      const r = await api<{ calendars: string[] }>("/api/apple/calendars");
      setCalendars(r.calendars);
      return r.calendars;
    } catch (e) {
      setMsg({ text: e instanceof ApiError ? e.message : "Calendar unreachable", tone: "alert" });
      return null;
    }
  };

  const save = async (next: AppleSettings) => {
    setBusy(true);
    setMsg(null);
    try {
      if (next.calendar_sync && !next.calendar) {
        const cals = calendars ?? (await loadCalendars());
        if (!cals?.length) {
          setBusy(false);
          return;
        }
        next = { ...next, calendar: cals[0] };
      }
      setCfg(await api<AppleSettings>("/api/settings/apple", { method: "PUT", body: JSON.stringify(next) }));
    } catch (e) {
      setMsg({ text: e instanceof ApiError ? e.message : "Backend unreachable", tone: "alert" });
    } finally {
      setBusy(false);
    }
  };

  const syncNow = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await post<{ events: number; notes: number }>("/api/apple/sync");
      setMsg({ text: `Synced ${r.events} events and ${r.notes} notes.`, tone: "ok" });
    } catch (e) {
      setMsg({ text: e instanceof ApiError ? e.message : "Sync failed", tone: "alert" });
    } finally {
      setBusy(false);
    }
  };

  if (!cfg) return null;
  return (
    <div className="card">
      <div className="panel-title">APPLE SYNC</div>
      <label className="toggle">
        <input
          type="checkbox"
          checked={cfg.calendar_sync}
          disabled={busy}
          onChange={(e) => save({ ...cfg, calendar_sync: e.target.checked })}
        />
        <span>Sync calendar with Apple Calendar</span>
      </label>
      {cfg.calendar_sync && (
        <div className="kv">
          <span>Calendar</span>
          <select
            className="field select"
            value={cfg.calendar}
            disabled={busy}
            onFocus={() => calendars === null && loadCalendars()}
            onChange={(e) => save({ ...cfg, calendar: e.target.value })}
          >
            {(calendars ?? [cfg.calendar]).map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </div>
      )}
      <label className="toggle">
        <input
          type="checkbox"
          checked={cfg.notes_sync}
          disabled={busy}
          onChange={(e) => save({ ...cfg, notes_sync: e.target.checked })}
        />
        <span>Sync notes with Apple Notes (folder “{cfg.notes_folder}”)</span>
      </label>
      {(cfg.calendar_sync || cfg.notes_sync) && (
        <button className="btn ghost card-btn" disabled={busy} onClick={syncNow}>
          SYNC EXISTING ITEMS NOW
        </button>
      )}
      {msg && <p className={`small tone-${msg.tone}`}>{msg.text}</p>}
      <p className="muted small">
        Off by default: everything stays encrypted inside the app. When on, new events and notes are also created in
        Apple's apps (and reach your other devices through iCloud), and calendar questions include your Apple
        calendars. macOS will ask once for permission to control Calendar and Notes.
      </p>
    </div>
  );
}
