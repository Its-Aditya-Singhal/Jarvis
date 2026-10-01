import { FormEvent, useEffect, useState } from "react";
import { ApiError, api, post } from "../lib/backend";
import { openUrl } from "../lib/open";
import { useStore } from "../lib/store";

type Service = "gmail" | "drive" | "calendar";

export interface GoogleSettings {
  client_id: string | null;
  has_secret: boolean;
  services: Record<Service, boolean>;
  connected: boolean;
  email: string | null;
  granted: Partial<Record<Service, boolean>>;
  connecting: boolean;
  error: string | null;
  console_url: string;
  url?: string;
}

const NAMES: Record<Service, string> = { gmail: "Gmail", drive: "Google Drive", calendar: "Google Calendar" };
const errText = (e: unknown) => (e instanceof ApiError ? e.message : "Backend unreachable");

/** Settings → Accounts: the owner's own Google OAuth client, which services, Connect/Disconnect. */
export default function GoogleCard() {
  const { googleVersion } = useStore();
  const [g, setG] = useState<GoogleSettings | null>(null);
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");
  const [json, setJson] = useState("");
  const [guide, setGuide] = useState(false);
  const [msg, setMsg] = useState<{ text: string; tone: "ok" | "alert" } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<GoogleSettings>("/api/settings/google")
      .then(setG)
      .catch((e) => setMsg({ text: errText(e), tone: "alert" }));
  }, [googleVersion]);

  // while the browser consent page is open, look again every two seconds
  useEffect(() => {
    if (!g?.connecting) return;
    const t = setInterval(() => {
      api<GoogleSettings>("/api/settings/google").then(setG).catch(() => undefined);
    }, 2000);
    return () => clearInterval(t);
  }, [g?.connecting]);

  const run = async (fn: () => Promise<GoogleSettings>, done?: string) => {
    setBusy(true);
    setMsg(null);
    try {
      const next = await fn();
      setG(next);
      if (done) setMsg({ text: done, tone: "ok" });
      return next;
    } catch (e) {
      setMsg({ text: errText(e), tone: "alert" });
      return null;
    } finally {
      setBusy(false);
    }
  };
  const put = (body: object) => api<GoogleSettings>("/api/settings/google", { method: "PUT", body: JSON.stringify(body) });

  const saveClient = (e: FormEvent) => {
    e.preventDefault();
    const body = json.trim() ? { client_json: json.trim() } : { client_id: clientId.trim(), client_secret: secret.trim() };
    run(() => put(body), "Client saved, encrypted on this Mac.").then((r) => {
      if (r) {
        setJson("");
        setSecret("");
        setClientId("");
      }
    });
  };
  const connect = async () => {
    const r = await run(() => post<GoogleSettings>("/api/settings/google/connect"));
    if (r?.url) {
      setMsg({ text: "Finish in your browser: pick your account and allow access.", tone: "ok" });
      await openUrl(r.url);
    }
  };

  if (!g) return msg ? <p className="error small">{msg.text}</p> : null;
  const hasClient = !!g.client_id && g.has_secret;
  const wanted = (Object.keys(NAMES) as Service[]).filter((s) => g.services[s]);
  return (
    <div className="card wide-card">
      <div className="panel-title">GOOGLE ACCOUNT</div>
      <div className="kv">
        <span>Status</span>
        <b className={g.connected ? "tone-ok" : g.connecting ? "tone-warn" : "muted"}>
          {g.connected ? `CONNECTED${g.email ? ` · ${g.email}` : ""}` : g.connecting ? "WAITING FOR THE BROWSER…" : "NOT CONNECTED"}
        </b>
      </div>

      {(Object.keys(NAMES) as Service[]).map((s) => (
        <label className="toggle" key={s}>
          <input
            type="checkbox"
            checked={g.services[s]}
            disabled={busy}
            onChange={(e) => run(() => put({ services: wanted.filter((w) => w !== s).concat(e.target.checked ? [s] : []) }))}
          />
          <span>
            {NAMES[s]}
            {g.connected && g.services[s] && !g.granted[s] && <em className="tone-warn"> · not allowed yet, reconnect</em>}
          </span>
        </label>
      ))}

      {!g.connected && (
        <>
          <button className="btn ghost card-btn" onClick={() => setGuide(!guide)} aria-expanded={guide}>
            {guide ? "HIDE SETUP STEPS" : "HOW TO SET UP (ONCE, ABOUT 5 MINUTES)"}
          </button>
          {guide && (
            <ol className="guide small">
              <li>Open Google Cloud Console and create a project (any name, e.g. “JARVIS”).</li>
              <li>APIs &amp; Services → Library: enable Gmail API, Google Drive API and Google Calendar API.</li>
              <li>
                OAuth consent screen: User type External, add your own address as a test user, then Publish app → In
                production (otherwise Google signs you out every 7 days). You'll see “Google hasn't verified this app”
                when connecting: it's your own app, choose Advanced → Continue.
              </li>
              <li>Credentials → Create credentials → OAuth client ID → Application type Desktop app.</li>
              <li>Paste the client ID and secret below (or the downloaded JSON), then press Connect.</li>
            </ol>
          )}
          <button className="btn ghost card-btn" onClick={() => openUrl(g.console_url)}>
            OPEN GOOGLE CLOUD CONSOLE
          </button>
          <form onSubmit={saveClient}>
            <label className="field-label" htmlFor="g-id">
              OAuth client ID {hasClient ? "· saved" : ""}
            </label>
            <input
              id="g-id"
              className="field"
              spellCheck={false}
              autoComplete="off"
              placeholder={g.client_id ?? "123…apps.googleusercontent.com"}
              value={clientId}
              maxLength={200}
              onChange={(e) => setClientId(e.target.value)}
            />
            <label className="field-label" htmlFor="g-secret">
              Client secret {g.has_secret ? "· saved (never shown)" : ""}
            </label>
            <input
              id="g-secret"
              className="field"
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={secret}
              maxLength={200}
              onChange={(e) => setSecret(e.target.value)}
            />
            <label className="field-label" htmlFor="g-json">
              …or paste the downloaded JSON file
            </label>
            <textarea
              id="g-json"
              className="field"
              rows={2}
              spellCheck={false}
              value={json}
              maxLength={20000}
              onChange={(e) => setJson(e.target.value)}
            />
            <button className="btn ghost card-btn" disabled={busy || !(json.trim() || (clientId.trim() && secret.trim()))}>
              SAVE CLIENT
            </button>
          </form>
        </>
      )}

      <div className="row-btns">
        {!g.connected && !g.connecting && (
          <button className="btn primary" disabled={busy || !hasClient || !wanted.length} onClick={connect}>
            CONNECT
          </button>
        )}
        {g.connecting && (
          <button className="btn ghost" disabled={busy} onClick={() => run(() => post<GoogleSettings>("/api/settings/google/cancel"))}>
            CANCEL
          </button>
        )}
        {g.connected && (
          <>
            {wanted.some((s) => !g.granted[s]) && (
              <button className="btn primary" disabled={busy} onClick={connect}>
                RECONNECT
              </button>
            )}
            <button
              className="btn ghost"
              disabled={busy}
              onClick={() => run(() => post<GoogleSettings>("/api/settings/google/disconnect"), "Disconnected; the token was revoked.")}
            >
              DISCONNECT
            </button>
          </>
        )}
        {hasClient && !g.connected && (
          <button className="btn ghost" disabled={busy} onClick={() => run(() => put({ remove_client: true }), "Client removed.")}>
            REMOVE CLIENT
          </button>
        )}
      </div>
      {(msg || g.error) && (
        <p className={`small ${msg?.tone === "ok" && !g.error ? "tone-ok" : "error"}`}>{g.error ?? msg?.text}</p>
      )}
      <p className="muted small">
        Lets you ask “summarize my last 10 emails”, “draft a mail to Rahul”, “what's on my calendar tomorrow” or “find
        the budget sheet in my Drive”. Drive is read-only. A mail is only sent after I read it back and you say yes (in
        your verified voice) or click Confirm. The sign-in token is encrypted with your Mac's Keychain key; only the
        mail, files or events a request needs are sent to the AI.
      </p>
    </div>
  );
}
