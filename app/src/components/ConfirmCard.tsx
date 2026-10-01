import { useEffect, useRef, useState } from "react";
import { ApiError, post } from "../lib/backend";
import { useStore } from "../lib/store";

/** A deletion waiting for the owner's go-ahead (spoken or clicked). */
export default function ConfirmCard() {
  const { confirm, auth } = useStore();
  const [now, setNow] = useState(() => Date.now());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const card = useRef<HTMLDivElement>(null);
  // a new confirmation below the fold (under a long conversation) is brought into view
  const id = confirm?.id;
  useEffect(() => {
    if (!id) return;
    // after the rest of the screen (timers, the reply) has settled
    const t = window.setTimeout(() => card.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }), 300);
    return () => window.clearTimeout(t);
  }, [id]);
  useEffect(() => {
    if (!confirm) return;
    setError(null);
    setNow(Date.now()); // the clock may be stale from before this confirmation arrived
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

  const email = confirm.tool === "email.send";
  return (
    <div className="confirm" ref={card}>
      <div className="confirm-head">
        <span>
          LEVEL 3 ·{" "}
          {/^(notes|calendar|memory)\./.test(confirm.tool)
            ? "CONFIRM DELETION"
            : confirm.tool === "mac.do"
              ? "CONFIRM SCRIPT"
              : email
                ? "CONFIRM EMAIL"
                : "CONFIRM"}
        </span>
        <span>{Math.ceil(left)} s</span>
      </div>
      {/* an email's spoken read-back is the draft itself: on screen it is shown once, below */}
      <div className="confirm-text">{email ? "Send this email? Say “yes, send it” or click Send." : confirm.text}</div>
      {confirm.detail && (
        <pre
          className={email ? "confirm-script confirm-mail" : "confirm-script"}
          aria-label={email ? "The email that will be sent" : "The script that will run"}
        >
          {confirm.detail}
        </pre>
      )}
      <div className="confirm-bar">
        <i style={{ width: `${(left / total) * 100}%` }} />
      </div>
      <div className="confirm-actions">
        <button className="btn danger" disabled={busy} onClick={() => answer(true)}>
          {email ? "SEND" : "CONFIRM"}
        </button>
        <button className="btn ghost" disabled={busy} onClick={() => answer(false)}>
          CANCEL
        </button>
      </div>
      {error && <p className="error small">{error}</p>}
    </div>
  );
}
