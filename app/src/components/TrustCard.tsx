import { useStore } from "../lib/store";

const LEVELS: [number, string, string][] = [
  [1, "READ", "Questions, calendar, notes & file search"],
  [2, "ACT", "Create events, notes, alarms · open apps"],
  [3, "CONFIRM", "Delete notes & events, after you confirm"],
];

const FEATURE_LABEL: Record<string, string> = {
  face_sim: "Face match",
  face_fresh: "Face freshness",
  face_quality: "Face quality",
  live_score: "Anti-spoof",
  live_fresh: "Liveness freshness",
  voice_present: "Voice heard",
  voice_sim: "Voice match",
  voice_fresh: "Voice freshness",
  others: "Other faces",
};

/** Current auth level, what blocks the next one, and the fusion classifier's inputs. */
export default function TrustCard() {
  const { auth } = useStore();
  const t = auth?.trust;
  const level = auth?.level ?? 0;
  return (
    <div className="card trust-card">
      <div className="panel-title">AUTH LEVEL · FUSION</div>
      <div className="ladder">
        {LEVELS.map(([n, name, what]) => {
          const reached = n <= 2 ? level >= n : !!t?.l3_ready;
          const blocker = t?.blockers[String(n)];
          return (
            <div key={n} className={`rung ${reached ? "on" : ""}`}>
              <b>L{n}</b>
              <div>
                <span className="rung-name">{name}</span>
                <span className="muted small">{what}</span>
                {!reached && t && blocker && <span className="rung-why">{blocker}</span>}
              </div>
            </div>
          );
        })}
      </div>
      {!t ? (
        <p className="muted small">Details are shown to the verified owner only.</p>
      ) : (
        <>
          <div className="kv">
            <span>Presence score · level 1</span>
            <b className={t.presence == null ? "" : t.presence >= 0.5 ? "tone-ok" : "tone-alert"}>
              {t.presence == null ? "rules only" : `${Math.round(t.presence * 100)}%`}
            </b>
          </div>
          <div className="kv">
            <span>Command score · levels 2–3</span>
            <b className={t.prob == null ? "" : t.prob >= 0.8 ? "tone-ok" : t.prob >= 0.5 ? "tone-warn" : "tone-alert"}>
              {t.prob == null ? "rules only" : `${Math.round(t.prob * 100)}%`}
            </b>
          </div>
          <div className="kv">
            <span>Voice window</span>
            <b>{t.voice_window_s == null ? "closed — speak to open" : `${Math.ceil(t.voice_window_s)} s left`}</b>
          </div>
          <div className="features">
            {Object.entries(t.features).map(([k, v]) => (
              <div key={k} className="feature">
                <span>{FEATURE_LABEL[k] ?? k}</span>
                <i>
                  <em style={{ width: `${Math.max(0, Math.min(1, v)) * 100}%` }} />
                </i>
                <b>{v.toFixed(2)}</b>
              </div>
            ))}
          </div>
          <p className="muted small">
            Hard rules come first (face + liveness, then your voice within a minute and nobody else in view); the fusion
            classifier ({t.model === "personal" ? "retrained on this Mac" : "shipped model"}) can only lower the level.
          </p>
        </>
      )}
    </div>
  );
}
