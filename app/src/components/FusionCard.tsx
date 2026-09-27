import { useEffect, useState } from "react";
import { ApiError, FusionInfo, api, post } from "../lib/backend";

const pct = (v: number) => `${(v * 100).toFixed(1)}%`;

/** The fusion classifier: how it was trained, how well it does, and personal retraining. */
export default function FusionCard() {
  const [info, setInfo] = useState<FusionInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api<FusionInfo>("/api/fusion")
      .then(setInfo)
      .catch((e) => setError(e instanceof ApiError ? e.message : "unavailable"));
  }, []);

  const run = async (path: string) => {
    setBusy(true);
    setError(null);
    try {
      setInfo(await post<FusionInfo>(path));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Backend unreachable");
    } finally {
      setBusy(false);
    }
  };

  const t = info?.test;
  const s = info?.training_samples;
  const d = info?.device_samples;
  return (
    <div className="card">
      <div className="panel-title">FUSION CLASSIFIER</div>
      {info && (
        <>
          <div className="kv">
            <span>Model</span>
            <b>
              {info.source === "personal"
                ? "Retrained on this Mac"
                : info.source === "default"
                  ? "Shipped"
                  : info.source}
            </b>
          </div>
          <div className="kv">
            <span>Type</span>
            <b>Logistic regression · 9 features + pairs</b>
          </div>
          {s && (
            <div className="kv">
              <span>Trained on</span>
              <b>
                {s.simulated.toLocaleString()} simulated
                {s.device_owner + s.device_other > 0 && ` + ${s.device_owner + s.device_other} real`}
              </b>
            </div>
          )}
          {t && (
            <>
              <div className="kv">
                <span>Test accuracy · ROC AUC</span>
                <b>
                  {pct(t.accuracy)} · {t.auc.toFixed(3)}
                </b>
              </div>
              <table className="rates">
                <thead>
                  <tr>
                    <th>Level</th>
                    <th>Threshold</th>
                    <th title="impostor samples accepted">False accept</th>
                    <th title="owner samples rejected">False reject</th>
                  </tr>
                </thead>
                <tbody>
                  {([1, 2, 3] as const).map((n) => {
                    const r = t[`level${n}`];
                    return (
                      <tr key={n}>
                        <td>L{n}</td>
                        <td>{r.threshold}</td>
                        <td>{pct(r.far)}</td>
                        <td>{pct(r.frr)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <p className="muted small">
                Fusion score alone, on held-out simulated sessions; the app also applies hard rules.
              </p>
            </>
          )}
          <div className="kv">
            <span>Samples from this Mac</span>
            <b>
              {d?.owner ?? 0} you · {d?.other ?? 0} other
            </b>
          </div>
          <p className="muted small">
            Only scores are kept (never images or audio): after your own commands, and when a stranger, spoof or unknown
            voice is caught. Retraining needs {info.min_owner_samples}+ of yours and your voice in the last minute.
          </p>
          <div className="row-btns">
            <button
              className="btn ghost"
              disabled={busy || (d?.owner ?? 0) < info.min_owner_samples}
              onClick={() => run("/api/fusion/retrain")}
            >
              RETRAIN WITH MY DATA
            </button>
            {info.source === "personal" && (
              <button className="btn ghost" disabled={busy} onClick={() => run("/api/fusion/reset")}>
                RESET
              </button>
            )}
          </div>
        </>
      )}
      {error && <p className="error small">{error}</p>}
    </div>
  );
}
