import { useEffect, useState } from "react";
import { ApiError, post } from "../lib/backend";
import { useStore } from "../lib/store";

/** A deletion waiting for the owner's go-ahead (spoken or clicked). */
export default function ConfirmCard() {
  const { confirm, auth } = useStore();
  const [now, setNow] = useState(() => Date.now());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!confirm) return;
    setError(null);
    const id = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(id);
  }, [confirm]);
  if (!confirm || (auth?.level ?? 0) < 1) return null;
  const left = Math.max(0, (confirm.expiresAt - now) / 1000);
  if (left <= 0) return null;
  const total = Math.max(confirm.expires_s, 1);

  const answer = async (accept: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const r = await post<{ ok: boolean; reply: string }>(`/api/confirm/${confirm.id}`, { accept });
      if (!r.ok) setError(r.reply);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Backend unreachable");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="confirm">
      <div className="confirm-head">
        <span>LEVEL 3 · {/^(notes|calendar|memory)\./.test(confirm.tool) ? "CONFIRM DELETION" : "CONFIRM"}</span>
        <span>{Math.ceil(left)} s</span>
      </div>
      <div className="confirm-text">{confirm.text}</div>
      <div className="confirm-bar">
        <i style={{ width: `${(left / total) * 100}%` }} />
      </div>
      <div className="confirm-actions">
        <button className="btn danger" disabled={busy} onClick={() => answer(true)}>
          CONFIRM
        </button>
        <button className="btn ghost" disabled={busy} onClick={() => answer(false)}>
          CANCEL
        </button>
      </div>
      {error && <p className="error small">{error}</p>}
    </div>
  );
}
