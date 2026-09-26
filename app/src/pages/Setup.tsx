import { FormEvent, useEffect, useState } from "react";
import CameraPreview from "../components/CameraPreview";
import Orb from "../components/Orb";
import { ApiError, Status, post } from "../lib/backend";
import { setStatus, useStore } from "../lib/store";

type Step = "welcome" | "names" | "face" | "done";

function initialStep(s: Status | null): Step {
  if (!s || !s.owner_name) return "welcome";
  if (!s.face_enrolled) return "face";
  return "done";
}

function Blocks({ value, cells = 24 }: { value: number; cells?: number }) {
  const filled = Math.round(value * cells);
  return (
    <div className="blocks" role="progressbar" aria-valuenow={Math.round(value * 100)} aria-valuemin={0} aria-valuemax={100}>
      {Array.from({ length: cells }, (_, i) => (
        <i key={i} className={i < filled ? "on" : ""} />
      ))}
      <b>{Math.round(value * 100)}%</b>
    </div>
  );
}

export default function Setup() {
  const { status } = useStore();
  const [step, setStep] = useState<Step>(() => initialStep(status));

  return (
    <div className="setup">
      <div className="setup-steps">
        {(["welcome", "names", "face", "done"] as Step[]).map((s, i) => (
          <span key={s} className={s === step ? "active" : ""}>
            {String(i + 1).padStart(2, "0")}
          </span>
        ))}
      </div>
      {step === "welcome" && <Welcome onNext={() => setStep("names")} />}
      {step === "names" && <Names status={status} onNext={() => setStep("face")} />}
      {step === "face" && <FaceEnroll onNext={() => setStep("done")} />}
      {step === "done" && <Done />}
    </div>
  );
}

function Welcome({ onNext }: { onNext: () => void }) {
  return (
    <section className="setup-card center">
      <Orb mode="idle" className="setup-orb" />
      <h1>Welcome.</h1>
      <p className="lead">Let's get to know you.</p>
      <p className="muted small">
        Everything you teach me stays on this Mac. Face data is stored only as encrypted
        mathematical embeddings — never as photos.
      </p>
      <button className="btn primary" onClick={onNext}>
        BEGIN SETUP
      </button>
    </section>
  );
}

function Names({ status, onNext }: { status: Status | null; onNext: () => void }) {
  const [owner, setOwner] = useState(status?.owner_name ?? "");
  const [assistant, setAssistant] = useState(status?.owner_name ? status.assistant_name : "JARVIS");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setStatus(await post<Status>("/api/setup/profile", { owner_name: owner, assistant_name: assistant }));
      onNext();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Backend unreachable");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="setup-card" onSubmit={submit}>
      <h2>Who should I call you?</h2>
      <input
        autoFocus
        className="field"
        value={owner}
        maxLength={40}
        placeholder="Your name"
        onChange={(e) => setOwner(e.target.value)}
      />
      <h2>And what would you like to call me?</h2>
      <p className="muted small">This also becomes my wake word once voice commands are enabled.</p>
      <input
        className="field"
        value={assistant}
        maxLength={24}
        placeholder="JARVIS, FRIDAY, EDITH…"
        onChange={(e) => setAssistant(e.target.value)}
      />
      {error && <p className="error">{error}</p>}
      <button className="btn primary" disabled={busy || !owner.trim() || !assistant.trim()}>
        CONTINUE
      </button>
    </form>
  );
}

function FaceEnroll({ onNext }: { onNext: () => void }) {
  const { enroll, status, enrollComplete } = useStore();
  const [started, setStarted] = useState(status?.mode === "enrolling");
  const [error, setError] = useState<string | null>(null);

  const done = enrollComplete || (status?.face_enrolled ?? false);
  useEffect(() => {
    if (done) {
      const id = window.setTimeout(onNext, 1400);
      return () => window.clearTimeout(id);
    }
  }, [done, onNext]);

  const start = async () => {
    setError(null);
    try {
      await post("/api/enroll/face/start");
      setStarted(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Backend unreachable");
    }
  };
  const restart = async () => {
    await post("/api/enroll/face/cancel").catch(() => {});
    setStarted(false);
  };

  const progress = done ? 1 : (enroll?.progress ?? 0);
  return (
    <section className="setup-card wide">
      <div className="enroll-grid">
        <div className="enroll-visual">
          <div className={`scan-ring ${started && !done ? "spinning" : ""} ${done ? "complete" : ""}`}>
            <CameraPreview round />
          </div>
        </div>
        <div className="enroll-text">
          <h2>Let's learn what you look like.</h2>
          {!started && !done && (
            <>
              <p className="muted">
                I'll ask you to turn and move a little so I can recognise you from different
                angles and distances. Sit in good light with only you in frame.
              </p>
              {error && <p className="error">{error}</p>}
              <button className="btn primary" onClick={start} disabled={status?.camera.status !== "active"}>
                {status?.camera.status === "active" ? "START FACE SCAN" : "WAITING FOR CAMERA…"}
              </button>
            </>
          )}
          {(started || done) && (
            <>
              <div className="enroll-label">FACE ENROLLMENT</div>
              <Blocks value={progress} />
              <p className="prompt">{done ? "Face profile captured." : (enroll?.prompt ?? "Preparing…")}</p>
              <p className="hint">{!done && enroll?.hint}</p>
              <div className="chips">
                {(enroll?.steps ?? []).map((s, i) => (
                  <span
                    key={s}
                    className={`chip ${done || i < (enroll?.step_index ?? 0) ? "done" : i === enroll?.step_index ? "now" : ""}`}
                  >
                    {s}
                  </span>
                ))}
              </div>
              {!done && (
                <button className="btn ghost" onClick={restart}>
                  RESTART
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}

function Done() {
  const { status } = useStore();
  const [error, setError] = useState<string | null>(null);
  const name = status?.assistant_name ?? "JARVIS";

  const enter = async () => {
    try {
      setStatus(await post<Status>("/api/setup/complete"));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Backend unreachable");
    }
  };

  const items: [string, boolean, string?][] = [
    ["Face enrolled", !!status?.face_enrolled],
    ["Security enabled — encrypted, local-only storage", true],
    ["Voice enrolled", false, "phase 2"],
    ["Liveness configured", false, "phase 3"],
    ["Local AI configured", false, "phase 5"],
  ];

  return (
    <section className="setup-card center">
      <h2>Your identity profile is ready.</h2>
      <ul className="checklist">
        {items.map(([label, ok, later]) => (
          <li key={label} className={ok ? "ok" : "pending"}>
            <i>{ok ? "✓" : "○"}</i>
            {label}
            {later && <em>coming in {later}</em>}
          </li>
        ))}
      </ul>
      {error && <p className="error">{error}</p>}
      <button className="btn primary" onClick={enter}>
        ENTER {name.toUpperCase()}
      </button>
    </section>
  );
}
