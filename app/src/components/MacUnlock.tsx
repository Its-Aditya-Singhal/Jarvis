import { useEffect, useState } from "react";
import { ApiError, post } from "../lib/backend";
import { useStore } from "../lib/store";

/** Voice-only: Settings changes need a voice match from the last minute. When the voice can't give
 *  one (an old voiceprint, a noisy room, nothing said yet), macOS's own password prompt can: it
 *  unlocks Settings for two minutes. JARVIS never sees the password. */
export default function MacUnlock({ onUnlocked }: { onUnlocked?: () => void }) {
  const { auth, status } = useStore();
  const [left, setLeft] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setLeft(auth?.mac_unlock_s ?? 0), [auth?.mac_unlock_s]);
  useEffect(() => {
    if (left <= 0) return;
    const id = window.setTimeout(() => setLeft((s) => Math.max(0, s - 1)), 1000);
    return () => window.clearTimeout(id);
  }, [left]);

  if (status?.face_auth !== false || !status?.setup_complete || (auth?.level ?? 0) >= 2) return null;

  const unlock = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await post<{ seconds: number }>("/api/unlock"); // macOS shows its own password prompt
      setLeft(r.seconds);
      onUnlocked?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Backend unreachable");
    } finally {
      setBusy(false);
    }
  };

  if (left > 0) {
    return <p className="note small">Unlocked with your Mac password · {left} s left to change settings.</p>;
  }
  return (
    <div className="note small mac-unlock">
      <span>
        Changing settings needs your voice: say “{status?.assistant_name ?? "Jarvis"}, hello” first, or use your
        Mac password.
      </span>
      <button className="btn ghost card-btn" onClick={unlock} disabled={busy}>
        {busy ? "WAITING FOR MACOS…" : "USE MAC PASSWORD"}
      </button>
      {error && <span className="error"> {error}</span>}
    </div>
  );
}
