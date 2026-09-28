import { useEffect, useState } from "react";
import { ApiError, PrivacyState, api, post } from "../lib/backend";
import { useStore } from "../lib/store";

const inTauri = "__TAURI_INTERNALS__" in window;
const errText = (e: unknown) => (e instanceof ApiError ? e.message : "Backend unreachable");
const fmt = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString([], { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }) : "";

const ACTION_LABEL: Record<string, string> = {
  delete_face: "DELETE",
  delete_voice: "DELETE",
  clear_memory: "FORGET ALL",
  clear_history: "DELETE ALL",
  clear_tools: "DELETE ALL",
  clear_security_log: "CLEAR",
  reset_fusion: "RESET",
};

/** What is stored, how it is protected, and how to take it back (verified owner only). */
export default function PrivacyPanel() {
  const { auth, privacyVersion } = useStore();
  const approved = auth?.state === "approved";
  const [data, setData] = useState<PrivacyState | null>(null);
  const [error, setError] = useState<string | null>(null);
  // kept apart from action errors: the 5 s refresh clears its own error, never an action's
  const [loadError, setLoadError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [erase, setErase] = useState("");

  const load = () =>
    api<PrivacyState>("/api/privacy")
      .then((d) => {
        setData(d);
        setLoadError(null);
      })
      .catch((e) => setLoadError(errText(e)));
  useEffect(() => {
    if (!approved) {
      setData(null);
      return;
    }
    load();
    const id = window.setInterval(load, 5000); // live network activity
    return () => window.clearInterval(id);
  }, [approved, privacyVersion]);

  const request = async (action: string, body?: object) => {
    setError(null);
    setNote(null);
    try {
      await post(`/api/privacy/${action}`, body);
      setNote("Confirm below — say “yes, go ahead” or click Confirm.");
    } catch (e) {
      setError(errText(e));
    }
  };

  const exportData = async () => {
    const stamp = new Date().toISOString().slice(0, 10);
    let path: string | null;
    if (inTauri) {
      try {
        const { save } = await import("@tauri-apps/plugin-dialog");
        path = await save({
          title: "Export my data",
          defaultPath: `assistant-export-${stamp}.json`,
          filters: [{ name: "JSON", extensions: ["json"] }],
        });
      } catch (e) {
        setError(`The save dialog didn't open: ${e instanceof Error ? e.message : String(e)}`);
        return;
      }
    } else {
      path = window.prompt("Full path for the export file", `~/Downloads/assistant-export-${stamp}.json`);
    }
    if (path) request("export", { path });
  };

  const setOffline = async (offline: boolean) => {
    setError(null);
    try {
      const r = await api<{ pending?: string }>("/api/settings/pref", {
        method: "PUT",
        body: JSON.stringify({ key: "privacy.offline", value: offline }),
      });
      if (r.pending) setNote("Allowing internet access needs a level-3 confirmation below.");
      load();
    } catch (e) {
      setError(errText(e));
    }
  };

  if (!approved) {
    return (
      <div className="view">
        <h2 className="view-title">PRIVACY</h2>
        <div className="card locked-card">Owner verification required to view or delete your data.</div>
      </div>
    );
  }

  const net = data?.network;
  return (
    <div className="view">
      <h2 className="view-title">PRIVACY</h2>
      {(error ?? loadError) && <p className="error small">{error ?? loadError}</p>}
      {note && <p className="note small">{note}</p>}
      {!data && !error && !loadError && <p className="muted small">Loading…</p>}
      <div className="cards" hidden={!data}>
        <div className="card wide-card">
          <div className="panel-title">WHAT I STORE ABOUT YOU</div>
          {data?.items.map((it) => (
            <div key={it.id} className="data-row">
              <div>
                <b>{it.title}</b>
                <span className="muted small">
                  {it.detail}
                  {it.updated && ` · updated ${fmt(it.updated)}`}
                </span>
                <span className="protection">{it.protection}</span>
              </div>
              {it.action && (
                <button
                  className="mini danger"
                  disabled={!it.present}
                  onClick={() => request(it.action!)}
                  title="Needs a level-3 confirmation"
                >
                  {ACTION_LABEL[it.action] ?? "DELETE"}
                </button>
              )}
            </div>
          ))}
          <p className="muted small">
            Stored in <code>{data?.location.replace(/^\/Users\/[^/]+/, "~")}</code> ·{" "}
            {Math.round((data?.db_bytes ?? 0) / 1024)} KB database · encryption key in the Keychain:{" "}
            {data?.keychain_key ? "present" : "missing"}. Every deletion needs level 3: a recent liveness check plus your
            spoken or clicked confirmation.
          </p>
        </div>

        <div className="card">
          <div className="panel-title">NEVER STORED</div>
          <ul className="plain-list">
            {data?.never_stored.map((t) => (
              <li key={t}>✕ {t}</li>
            ))}
          </ul>
        </div>

        <div className="card">
          <div className="panel-title">NETWORK</div>
          <div className="kv">
            <span>Internet access</span>
            <b className={net?.offline ? "tone-ok" : "tone-warn"}>{net?.offline ? "BLOCKED · OFFLINE" : "ALLOWED"}</b>
          </div>
          <div className="kv">
            <span>Open connections to the internet</span>
            <b className={net?.external.length ? "tone-alert" : "tone-ok"}>{net?.external.length ?? 0}</b>
          </div>
          {net?.external.map((c, i) => (
            <div key={i} className="kv item">
              <span className="mono">
                {c.process} → {c.host}:{c.port}
                {c.count > 1 ? ` ×${c.count}` : ""}
              </span>
              <b>{c.status}</b>
            </div>
          ))}
          <div className="kv">
            <span>Attempts {net?.offline ? "blocked" : "seen"}</span>
            <b>{net?.attempts.length ?? 0}</b>
          </div>
          {net?.attempts.slice(0, 5).map((a, i) => (
            <div key={i} className="kv item">
              <span className="mono">
                {a.what} {a.host}
                {a.port ? `:${a.port}` : ""}
              </span>
              <b className={a.blocked ? "tone-ok" : "tone-warn"}>{a.blocked ? "BLOCKED" : "ALLOWED"}</b>
            </div>
          ))}
          <button className="btn ghost card-btn" onClick={() => setOffline(!net?.offline)}>
            {net?.offline ? "ALLOW INTERNET ACCESS" : "BLOCK INTERNET ACCESS"}
          </button>
          <p className="muted small">
            Everything runs on this Mac; the app only talks to itself and to Ollama on 127.0.0.1. Offline mode blocks
            any other connection from the assistant's backend. Connections of the Ollama server are shown too.
          </p>
        </div>

        <div className="card">
          <div className="panel-title">EXPORT</div>
          <p className="muted small">
            A readable JSON copy of your memory, history, notes, alarms, events, settings and security log. Face and
            voice templates are never exported. The file is readable only by your macOS account.
          </p>
          <button className="btn ghost card-btn" onClick={exportData}>
            EXPORT MY DATA
          </button>
        </div>

        <div className="card danger-card">
          <div className="panel-title">FACTORY RESET</div>
          <p className="muted small">
            Erases profiles, memory, history, notes, settings and the security log, and destroys the encryption key so
            nothing left on disk can be read. You'll go through setup again.
          </p>
          <input className="field" value={erase} placeholder="Type ERASE" onChange={(e) => setErase(e.target.value)} />
          <button
            className="btn danger card-btn"
            disabled={erase !== "ERASE"}
            onClick={() => {
              setErase("");
              request("factory_reset");
            }}
          >
            ERASE EVERYTHING
          </button>
        </div>
      </div>
    </div>
  );
}
