import { useState } from "react";
import Orb from "../components/Orb";
import { ApiError, Status, post } from "../lib/backend";
import { refreshStatus, setStatus, useStore } from "../lib/store";
import { FaceEnroll } from "./Setup";

/** Shown after setup when the face profile was deleted, or a confirmed re-scan is running. */
export default function FaceRescan() {
  const { status, speaking } = useStore();
  const r = status?.face_reenroll;
  const [error, setError] = useState<string | null>(null);
  const [erase, setErase] = useState("");
  if (!r) return null;
  const name = status?.assistant_name ?? "JARVIS";

  const keep = async () => {
    await post("/api/enroll/face/keep").catch(() => {});
    refreshStatus();
  };
  const reset = async () => {
    setError(null);
    try {
      setStatus(await post<Status>("/api/recovery/reset"));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Backend unreachable");
    }
  };

  if (r.allowed) {
    const title =
      r.kind === "redo"
        ? "Let's re-scan your face."
        : r.kind === "deleted"
          ? "Face profile deleted — let's scan it again."
          : "Voice recognised — let's scan your face.";
    return (
      <div className="setup">
        <FaceEnroll rescan title={title} onNext={refreshStatus} />
        {r.kind === "redo" && status?.face_enrolled && (
          <button className="btn ghost rescan-keep" onClick={keep}>
            KEEP MY CURRENT PROFILE
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="setup">
      <section className="setup-card center">
        <Orb mode={speaking ? "scanning" : "locked"} listen className="setup-orb" />
        <h2>No face profile</h2>
        <p className="lead">{r.reason}.</p>
        {!r.locked_out ? (
          <p className="muted small">
            Say “{name}, it's me, please let me scan my face” — once I recognise your voice you can scan your face
            again. Your memory and data are still here.
          </p>
        ) : (
          <>
            <p className="muted small">
              Without a face or voice profile nobody can be verified, so the only way forward is to erase everything
              (memory, history, notes, settings and the encryption key) and set up again. Nothing is revealed.
            </p>
            <input
              className="field"
              value={erase}
              placeholder="Type ERASE to confirm"
              onChange={(e) => setErase(e.target.value)}
            />
            <button className="btn danger" disabled={erase !== "ERASE"} onClick={reset}>
              ERASE AND START OVER
            </button>
          </>
        )}
        {error && <p className="error">{error}</p>}
      </section>
    </div>
  );
}
