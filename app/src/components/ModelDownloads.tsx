import { useEffect, useRef, useState } from "react";
import { ApiError, ModelPack, ModelsState, api, post } from "../lib/backend";
import { openUrl } from "../lib/open";

const errText = (e: unknown) => (e instanceof ApiError ? e.message : "Backend unreachable");

export const gb = (n: number) => (n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(n / 1e6))} MB`);

const STAGE: Record<string, string> = {
  downloading: "DOWNLOADING",
  verifying: "CHECKING SHA-256",
  unpacking: "UNPACKING",
};

function Bar({ value, label }: { value: number; label: string }) {
  const pct = Math.max(0, Math.min(100, Math.round(value * 100)));
  return (
    <div className="dl-bar" role="progressbar" aria-label={label} aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
      <div className="dl-fill" style={{ transform: `scaleX(${pct / 100})` }} />
    </div>
  );
}

function eta(left: number, bps: number): string {
  if (bps < 1) return "";
  const s = left / bps;
  if (s < 60) return "under a minute left";
  const m = Math.round(s / 60);
  return m < 60 ? `about ${m} min left` : `about ${Math.floor(m / 60)} h ${m % 60} min left`;
}

function PackRow({ p, active, onGet, busy }: { p: ModelPack; active: boolean; onGet?: () => void; busy: boolean }) {
  const state = p.installed ? "READY" : active ? "IN PROGRESS" : p.partial ? "PAUSED" : p.required ? "NEEDED" : "OPTIONAL";
  const tone = p.installed ? "tone-ok" : active ? "tone-accent" : p.required ? "tone-warn" : "muted";
  return (
    <li className="dl-pack">
      <div>
        <b>{p.title}</b>
        <span className="muted small">{p.detail}</span>
      </div>
      <span className="muted small mono">{gb(p.installed ? p.size : p.remaining || p.size)}</span>
      {onGet && !p.installed && !active ? (
        <button className="btn ghost small-btn" disabled={busy} onClick={onGet}>
          {p.partial ? "RESUME" : "DOWNLOAD"}
        </button>
      ) : (
        <span className={`mono small ${tone}`}>{state}</span>
      )}
    </li>
  );
}

/**
 * The on-device models: what's there, downloading what's missing (resumable, checksummed),
 * and the local language model through Ollama. `firstRun` shows only what's required and
 * ends with a restart; otherwise every pack (e.g. Whisper medium) can be added.
 */
export default function ModelDownloads({ firstRun = false }: { firstRun?: boolean }) {
  const [m, setM] = useState<ModelsState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [fetched, setFetched] = useState(false); // something new arrived: a restart loads it
  const wasRunning = useRef(false);

  const load = () =>
    api<ModelsState>("/api/models")
      .then((s) => {
        setM(s);
        const running = s.files.state === "downloading" || s.files.state === "verifying" || s.files.state === "unpacking";
        if (wasRunning.current && s.files.state === "done") setFetched(true);
        wasRunning.current = running;
      })
      .catch((e) => setError(errText(e)));
  useEffect(() => {
    load();
    const id = window.setInterval(load, 1000);
    return () => window.clearInterval(id);
  }, []);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      const r = (await fn()) as { started?: boolean; reason?: string } | undefined;
      if (r && r.started === false && r.reason && r.reason !== "already downloaded") setError(r.reason);
      await load();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const restart = async () => {
    setRestarting(true);
    try {
      await post("/api/models/restart");
    } catch (e) {
      setRestarting(false);
      setError(errText(e));
    }
  };

  if (!m) {
    return error ? <p className="error small">{error}</p> : <p className="muted small">Checking the models on this Mac…</p>;
  }
  const f = m.files;
  const o = m.ollama;
  const running = f.state === "downloading" || f.state === "verifying" || f.state === "unpacking";
  const packs = firstRun ? f.packs.filter((p) => p.required) : f.packs;
  const needed = f.needed.length > 0;
  const tooFull = needed && f.free_bytes < f.needed_bytes + 500e6;
  const anyPartial = packs.some((p) => p.partial && !p.installed);
  const pulling = o.pull.state === "downloading";
  const progress = f.total_bytes ? f.done_bytes / f.total_bytes : 0;

  return (
    <div className="dl">
      {needed && (
        <p className={`small ${tooFull ? "tone-alert" : "muted"}`}>
          {gb(f.needed_bytes)} to download · {gb(f.free_bytes)} free on this Mac
          {tooFull ? " — free some space first." : "."}
        </p>
      )}
      <ul className="dl-packs">
        {packs.map((p) => (
          <PackRow
            key={p.id}
            p={p}
            busy={busy || running}
            active={running && f.queued.includes(p.id) && !p.installed}
            onGet={firstRun ? undefined : () => act(() => post("/api/models/download", { packs: [p.id] }))}
          />
        ))}
      </ul>

      {running && (
        <div className="dl-progress" aria-live="polite">
          <div className="dl-line">
            <span className="mono small tone-accent">{STAGE[f.state]}</span>
            <span className="muted small mono">
              {gb(f.done_bytes)} / {gb(f.total_bytes)}
              {f.state === "downloading" && f.speed_bps > 0 ? ` · ${gb(f.speed_bps)}/s · ${eta(f.total_bytes - f.done_bytes, f.speed_bps)}` : ""}
            </span>
          </div>
          <Bar value={progress} label="Model download" />
          {f.file && <p className="muted small mono">{f.file.split("/").pop()}</p>}
        </div>
      )}
      {f.state === "error" && f.error && <p className="error small">{f.error}</p>}
      {f.state === "cancelled" && <p className="muted small">Paused. What arrived is kept: press Resume to continue.</p>}
      {error && <p className="error small">{error}</p>}

      <div className="row">
        {running ? (
          <button className="btn ghost" disabled={busy} onClick={() => act(() => post("/api/models/cancel"))}>
            PAUSE
          </button>
        ) : (
          needed && (
            <button className="btn primary" disabled={busy || tooFull} onClick={() => act(() => post("/api/models/download"))}>
              {anyPartial || f.state === "error" || f.state === "cancelled" ? "RESUME DOWNLOAD" : `DOWNLOAD ${gb(f.needed_bytes)}`}
            </button>
          )
        )}
      </div>

      <OllamaSection o={o} busy={busy} act={act} />

      {(firstRun ? !needed : fetched) && (
        <div className="dl-done">
          <p className="small">
            {firstRun
              ? o.ready
                ? "Everything is on this Mac. From here on, nothing needs the internet."
                : "The core models are ready. You can add the language model now or later in Settings → Models: until then, instant commands still work."
              : "New models downloaded. Restart the assistant to use them."}
          </p>
          <button className="btn primary" disabled={restarting || pulling || running} onClick={restart}>
            {restarting ? "RESTARTING…" : pulling ? "WAIT FOR THE LANGUAGE MODEL…" : firstRun ? "CONTINUE" : "RESTART NOW"}
          </button>
        </div>
      )}
    </div>
  );
}

function OllamaSection({ o, busy, act }: { o: ModelsState["ollama"]; busy: boolean; act: (fn: () => Promise<unknown>) => void }) {
  const [copied, setCopied] = useState(false);
  const pull = o.pull;
  const pulling = pull.state === "downloading";
  return (
    <div className="dl-ollama">
      <div className="panel-title flush">LANGUAGE MODEL · OLLAMA</div>
      {!o.installed ? (
        <>
          <p className="small">
            Questions and free-form requests use a local language model through Ollama, a free app. Install it, then
            press Check again.
          </p>
          <div className="row">
            <button className="btn ghost" onClick={() => openUrl(o.install.url)}>
              GET OLLAMA
            </button>
            <button
              className="btn ghost"
              onClick={() => {
                navigator.clipboard?.writeText(o.install.brew).then(() => setCopied(true), () => undefined);
              }}
              title="Copy the Homebrew command"
            >
              {copied ? "COPIED" : <code>{o.install.brew}</code>}
            </button>
            <button className="btn ghost" disabled={busy} onClick={() => act(() => post("/api/models/ollama/start"))}>
              CHECK AGAIN
            </button>
          </div>
        </>
      ) : !o.running ? (
        <>
          <p className="small">Ollama is installed but not running.</p>
          {o.error && <p className="muted small">{o.error}</p>}
          <button className="btn ghost" disabled={busy} onClick={() => act(() => post("/api/models/ollama/start"))}>
            START OLLAMA
          </button>
        </>
      ) : (
        <ul className="dl-packs">
          {o.models.map((mod) => {
            const active = pulling && pull.model === mod.name;
            return (
              <li className="dl-pack" key={mod.name}>
                <div>
                  <b>{mod.name}</b>
                  <span className="muted small">
                    {mod.purpose}
                    {mod.required ? "" : " (optional)"}
                  </span>
                  {active && (
                    <>
                      <Bar value={pull.total ? pull.completed / pull.total : 0} label={`Downloading ${mod.name}`} />
                      <span className="muted small mono">
                        {pull.status}
                        {pull.total ? ` · ${gb(pull.completed)} / ${gb(pull.total)}` : ""}
                      </span>
                    </>
                  )}
                </div>
                <span className="muted small mono">{mod.size_gb ? `${mod.size_gb} GB` : ""}</span>
                {mod.installed ? (
                  <span className="mono small tone-ok">READY</span>
                ) : active ? (
                  <span className="mono small tone-accent">PULLING</span>
                ) : (
                  <button
                    className="btn ghost small-btn"
                    disabled={busy || pulling}
                    onClick={() => act(() => post("/api/models/ollama/pull", { model: mod.name }))}
                  >
                    DOWNLOAD
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {pull.state === "error" && pull.error && <p className="error small">{pull.error}</p>}
    </div>
  );
}
