import { FormEvent, useEffect, useState } from "react";
import { ApiError, api, post } from "../lib/backend";
import { openUrl } from "../lib/open";

export interface AiSettings {
  provider: "gemini" | "ollama" | "off";
  fast_model: string | null;
  heavy_model: string | null;
  key: string | null; // "…ab12" or null: the key itself never comes back
  key_page: string;
  status: string;
}

const errText = (e: unknown) => (e instanceof ApiError ? e.message : "Backend unreachable");

/** The brain: Gemini API key and the two model names (owner only; changes need level 2). */
export default function AiCard() {
  const [ai, setAi] = useState<AiSettings | null>(null);
  const [key, setKey] = useState("");
  const [fast, setFast] = useState("");
  const [heavy, setHeavy] = useState("");
  const [msg, setMsg] = useState<{ text: string; tone: "ok" | "alert" } | null>(null);
  const [busy, setBusy] = useState(false);

  const show = (a: AiSettings) => {
    setAi(a);
    setFast(a.fast_model ?? "");
    setHeavy(a.heavy_model ?? "");
  };
  useEffect(() => {
    api<AiSettings>("/api/settings/ai")
      .then(show)
      .catch((e) => setMsg({ text: errText(e), tone: "alert" }));
  }, []);

  const save = async (body: Record<string, string>, done: string) => {
    setBusy(true);
    setMsg(null);
    try {
      show(await api<AiSettings>("/api/settings/ai", { method: "PUT", body: JSON.stringify(body) }));
      setKey("");
      setMsg({ text: done, tone: "ok" });
    } catch (e) {
      setMsg({ text: errText(e), tone: "alert" });
    } finally {
      setBusy(false);
    }
  };
  const test = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await post<{ fast: string; heavy: string }>("/api/settings/ai/test");
      const ok = r.fast.startsWith("ok") && r.heavy.startsWith("ok");
      setMsg({
        text: ok ? "Both models answered. You're set." : `Commands model: ${r.fast} · Writing model: ${r.heavy}`,
        tone: ok ? "ok" : "alert",
      });
    } catch (e) {
      setMsg({ text: errText(e), tone: "alert" });
    } finally {
      setBusy(false);
    }
  };

  if (!ai) return null;
  const gemini = ai.provider === "gemini";
  const models = fast !== (ai.fast_model ?? "") || heavy !== (ai.heavy_model ?? "");
  return (
    <div className="card wide-card">
      <div className="panel-title">AI</div>
      <div className="pref">
        <span>Brain</span>
        <div className="seg">
          <button className={gemini ? "on" : ""} disabled={busy} onClick={() => !gemini && save({ provider: "gemini" }, "Using Gemini.")}>
            Gemini (cloud, light)
          </button>
          <button
            className={!gemini ? "on" : ""}
            disabled={busy}
            onClick={() => gemini && save({ provider: "ollama" }, "Using the local model through Ollama.")}
          >
            Local (Ollama, heavy)
          </button>
        </div>
      </div>
      <div className="kv">
        <span>Status</span>
        <b className={ai.status === "ready" ? "tone-ok" : "tone-alert"}>{ai.status === "ready" ? "READY" : ai.status}</b>
      </div>
      {gemini && (
        <>
          <form
            onSubmit={(e: FormEvent) => {
              e.preventDefault();
              if (key.trim()) save({ api_key: key.trim() }, "Key saved, encrypted on this Mac.");
            }}
          >
            <label className="field-label" htmlFor="ai-key">
              Gemini API key {ai.key ? `· saved (${ai.key})` : "· not set"}
            </label>
            <div className="row">
              <input
                id="ai-key"
                className="field"
                type="password"
                autoComplete="off"
                spellCheck={false}
                placeholder={ai.key ? "Paste a new key to replace it" : "Paste your key from Google AI Studio"}
                value={key}
                maxLength={200}
                onChange={(e) => setKey(e.target.value)}
              />
              <button className="btn primary" disabled={busy || !key.trim()}>
                SAVE KEY
              </button>
            </div>
          </form>
          <div className="row-btns">
            <button className="btn ghost" onClick={() => openUrl(ai.key_page)}>
              GET A FREE KEY
            </button>
            <button className="btn ghost" disabled={busy || !ai.key} onClick={test}>
              TEST
            </button>
            {ai.key && (
              <button className="btn ghost" disabled={busy} onClick={() => save({ api_key: "" }, "Key removed.")}>
                REMOVE KEY
              </button>
            )}
          </div>
          <label className="field-label" htmlFor="ai-fast">
            Commands model (fast)
          </label>
          <input id="ai-fast" className="field" value={fast} spellCheck={false} onChange={(e) => setFast(e.target.value.trim())} />
          <label className="field-label" htmlFor="ai-heavy">
            Writing model (email summaries, drafts)
          </label>
          <input id="ai-heavy" className="field" value={heavy} spellCheck={false} onChange={(e) => setHeavy(e.target.value.trim())} />
          {models && (
            <button className="btn primary card-btn" disabled={busy} onClick={() => save({ fast_model: fast, heavy_model: heavy }, "Models saved.")}>
              SAVE MODELS
            </button>
          )}
        </>
      )}
      {msg && <p className={`small ${msg.tone === "ok" ? "tone-ok" : "error"}`}>{msg.text}</p>}
      <p className="muted small">
        {gemini
          ? "Everyday commands (volume, brightness, timers, music, screenshots) never use the AI. Everything else goes to Google's free Gemini API: the commands model first, the writing model if its free limit runs out, and the other way round for writing. Google may use free-tier requests to improve its products. The key is encrypted with your Mac's Keychain key and never shown again."
          : "The local model runs on this Mac through Ollama and uses several GB of memory while it answers."}
      </p>
    </div>
  );
}
