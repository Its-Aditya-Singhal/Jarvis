import { useEffect, useState } from "react";
import { ApiError, post } from "../lib/backend";
import { resetVoiceEnroll, useStore } from "../lib/store";
import { useLatest } from "../lib/useLatest";
import Waveform from "./Waveform";

interface Props {
  onDone: () => void;
  /** offered only when voice can't work on this machine (no mic / model) */
  onSkip?: () => void;
  onClose?: () => void;
  title?: string;
}

export default function VoiceEnroll({ onDone, onSkip, onClose, title = "Now let's learn your voice." }: Props) {
  const { voiceEnroll, voiceEnrollComplete, voiceEnrollCancelled, speaking, status } = useStore();
  const [started, setStarted] = useState(status?.voice_mode === "enrolling");
  const [error, setError] = useState<string | null>(null);

  const onDoneRef = useLatest(onDone);
  useEffect(() => resetVoiceEnroll, []);
  useEffect(() => {
    if (voiceEnrollComplete) {
      const id = window.setTimeout(() => onDoneRef.current(), 1400);
      return () => window.clearTimeout(id);
    }
  }, [voiceEnrollComplete, onDoneRef]);
  useEffect(() => {
    if (voiceEnrollCancelled) setStarted(false);
  }, [voiceEnrollCancelled]);

  const mic = status?.mic;
  const voiceModel = status?.models.voice;
  const unavailable = mic?.status === "error" || (voiceModel !== undefined && voiceModel !== "ready" && voiceModel !== "loading");

  const start = async () => {
    setError(null);
    resetVoiceEnroll();
    try {
      await post("/api/enroll/voice/start");
      setStarted(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Backend unreachable");
    }
  };
  const cancel = async () => {
    await post("/api/enroll/voice/cancel").catch(() => {});
    setStarted(false);
    resetVoiceEnroll();
  };

  const snap = voiceEnroll;
  const done = voiceEnrollComplete;
  const progress = done ? 1 : (snap?.progress ?? 0);
  const cells = 24;

  return (
    <div className="enroll-grid">
      <div className="voice-visual">
        <div className={`voice-orb ${speaking ? "hot" : ""} ${done ? "complete" : ""}`}>
          <Waveform active={speaking} />
        </div>
        <div className="mic-line">
          <i className={`dot ${mic?.status === "active" ? "tone-ok" : "tone-alert"}`} />
          {mic?.status === "active" ? (mic.device ?? "Microphone") : (mic?.error ?? "Microphone starting…")}
        </div>
        <div className={`listening ${speaking && started ? "on" : ""}`}>● HEARING YOU</div>
      </div>

      <div className="enroll-text">
        <h2>{title}</h2>
        {!started && !done && (
          <>
            <p className="muted">
              I'll show you six short phrases in English, Hindi and Hinglish. Read each one aloud at a
              normal pace. Only a mathematical voiceprint is kept — never the recording.
            </p>
            {voiceEnrollCancelled && <p className="error">{voiceEnrollCancelled}</p>}
            {error && <p className="error">{error}</p>}
            <div className="row">
              <button className="btn primary" onClick={start} disabled={mic?.status !== "active" || voiceModel !== "ready"}>
                {mic?.status !== "active" ? "WAITING FOR MICROPHONE…" : voiceModel !== "ready" ? "LOADING VOICE MODEL…" : "START VOICE SCAN"}
              </button>
              {onClose && (
                <button className="btn ghost" onClick={onClose}>
                  CLOSE
                </button>
              )}
            </div>
            {unavailable && onSkip && (
              <button className="btn ghost" onClick={onSkip}>
                SKIP — VOICE UNAVAILABLE ON THIS MAC
              </button>
            )}
          </>
        )}
        {(started || done) && (
          <>
            <div className="enroll-label">VOICE ENROLLMENT</div>
            <div className="blocks">
              {Array.from({ length: cells }, (_, i) => (
                <i key={i} className={i < Math.round(progress * cells) ? "on" : ""} />
              ))}
              <b>{Math.round(progress * 100)}%</b>
            </div>
            {done ? (
              <p className="prompt">Voice profile captured.</p>
            ) : (
              <div className={`phrase-card ${snap?.accepted ? "flash" : ""}`} key={snap?.index}>
                <span className="lang">{snap?.lang}</span>
                <p className="phrase">“{snap?.text ?? "…"}”</p>
                <span className="muted small">Read this aloud</span>
              </div>
            )}
            <p className="hint">{!done && snap?.hint}</p>
            <ol className="phrase-list">
              {(snap?.phrases ?? []).map((p, i) => (
                <li key={i} className={done || i < (snap?.index ?? 0) ? "done" : i === snap?.index ? "now" : ""}>
                  <em>{p.lang}</em>
                  {p.text}
                </li>
              ))}
            </ol>
            {!done && (
              <button className="btn ghost" onClick={cancel}>
                RESTART
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}
