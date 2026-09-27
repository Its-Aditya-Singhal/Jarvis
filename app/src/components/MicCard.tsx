import { useEffect, useRef, useState } from "react";
import { ApiError, MicState, api, post } from "../lib/backend";
import { getMicLevel } from "../lib/store";

const errText = (e: unknown) => (e instanceof ApiError ? e.message : "Backend unreachable");

const PERMISSION: Record<MicState["permission"], string> = {
  granted: "allowed",
  denied: "blocked",
  restricted: "blocked by a device policy",
  not_asked: "not asked yet",
  unknown: "unknown",
};

/** Live input level, drawn every frame without re-rendering React. */
function LevelBar({ on }: { on: boolean }) {
  const bar = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let id = 0;
    const tick = () => {
      if (bar.current) bar.current.style.transform = `scaleX(${on ? getMicLevel() : 0})`;
      id = requestAnimationFrame(tick);
    };
    id = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(id);
  }, [on]);
  return (
    <div className="level-track" aria-hidden>
      <div className="level-fill" ref={bar} />
    </div>
  );
}

/** Which microphone is used, whether it works, and why not (owner only). */
export default function MicCard() {
  const [mic, setMic] = useState<MicState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () =>
    api<MicState>("/api/mic")
      .then((m) => {
        setMic(m);
        setError(null);
      })
      .catch((e) => setError(errText(e)));
  useEffect(() => {
    load();
    const id = window.setInterval(load, 3000); // plugging a mic in, granting permission…
    return () => window.clearInterval(id);
  }, []);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
      window.setTimeout(load, 600); // the stream takes a moment to reopen
    }
  };
  const choose = (device: string) =>
    act(() => api("/api/mic", { method: "PUT", body: JSON.stringify({ device: device || null }) }));

  const ok = mic?.status === "active";
  const tone = ok ? "tone-ok" : mic?.status === "error" ? "tone-alert" : "tone-warn";
  return (
    <div className="card">
      <div className="panel-title">MICROPHONE</div>
      <div className="kv">
        <span>Status</span>
        <b className={tone}>{ok ? "LISTENING" : mic?.status === "error" ? "NOT WORKING" : "STARTING…"}</b>
      </div>
      <LevelBar on={ok} />
      {mic?.status === "error" && (
        <>
          <p className="error small">{mic.error}</p>
          {mic.fix && <p className="note small">{mic.fix}</p>}
          {mic.detail && <p className="muted small mono">{mic.detail}</p>}
        </>
      )}
      {mic?.fallback && (
        <p className="note small">
          {mic.chosen} isn't connected — using {mic.device} until it's back.
        </p>
      )}
      <div className="kv">
        <span>Input</span>
        <select
          className="field select"
          aria-label="Microphone"
          value={mic?.chosen ?? ""}
          disabled={busy || !mic}
          onChange={(e) => choose(e.target.value)}
        >
          <option value="">
            System default{mic?.devices.find((d) => d.default) ? ` (${mic.devices.find((d) => d.default)!.name})` : ""}
          </option>
          {mic?.chosen && !mic.devices.some((d) => d.name === mic.chosen) && (
            <option value={mic.chosen}>{mic.chosen} (not connected)</option>
          )}
          {mic?.devices.map((d) => (
            <option key={d.name} value={d.name}>
              {d.name}
            </option>
          ))}
        </select>
      </div>
      <div className="kv">
        <span>macOS permission for {mic?.app ?? "the app"}</span>
        <b className={mic?.permission === "granted" ? "tone-ok" : mic?.permission === "unknown" ? "" : "tone-alert"}>
          {mic ? PERMISSION[mic.permission] : "—"}
        </b>
      </div>
      {error && <p className="error small">{error}</p>}
      <button className="btn ghost card-btn" disabled={busy} onClick={() => act(() => post("/api/mic/retry"))}>
        RETRY MICROPHONE
      </button>
      <p className="muted small">
        Changing the microphone needs level 2 — another input could be used to play recordings to voice
        verification.
      </p>
    </div>
  );
}
