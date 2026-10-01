import { FormEvent, useEffect, useState } from "react";
import { ApiError, api, post } from "../lib/backend";
import { openUrl } from "../lib/open";

export interface BedrockSettings {
  key: string | null; // "…ab12" or null: the key itself never comes back
  region: string;
  fast_model: string;
  heavy_model: string;
  models: string[]; // suggestions; any model id the AWS account can use works
  key_page: string;
  access_page: string;
}

export interface AiSettings {
  provider: "gemini" | "bedrock" | "ollama" | "off";
  fast_model: string | null;
  heavy_model: string | null;
  key: string | null; // "…ab12" or null: the key itself never comes back
  key_page: string;
  bedrock?: BedrockSettings;
  status: string;
}

type Provider = "gemini" | "bedrock" | "ollama";

const errText = (e: unknown) => (e instanceof ApiError ? e.message : "Backend unreachable");

/** The brain: Gemini (free), Claude on Bedrock (paid) or a local model; keys and model names
 * (owner only; changes need level 2). */
export default function AiCard() {
  const [ai, setAi] = useState<AiSettings | null>(null);
  const [key, setKey] = useState("");
  const [fast, setFast] = useState("");
  const [heavy, setHeavy] = useState("");
  const [bKey, setBKey] = useState("");
  const [bRegion, setBRegion] = useState("");
  const [bFast, setBFast] = useState("");
  const [bHeavy, setBHeavy] = useState("");
  const [msg, setMsg] = useState<{ text: string; tone: "ok" | "alert" } | null>(null);
  const [busy, setBusy] = useState(false);

  const show = (a: AiSettings) => {
    setAi(a);
    setFast(a.fast_model ?? "");
    setHeavy(a.heavy_model ?? "");
    setBRegion(a.bedrock?.region ?? "");
    setBFast(a.bedrock?.fast_model ?? "");
    setBHeavy(a.bedrock?.heavy_model ?? "");
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
      setBKey("");
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
  const provider: Provider = ai.provider === "off" ? "gemini" : ai.provider;
  const gemini = provider === "gemini";
  const bedrock = provider === "bedrock" ? ai.bedrock : undefined;
  const pick = (p: Provider, done: string) => provider !== p && save({ provider: p }, done);
  const models = fast !== (ai.fast_model ?? "") || heavy !== (ai.heavy_model ?? "");
  const bChanged = bedrock && (bRegion !== bedrock.region || bFast !== bedrock.fast_model || bHeavy !== bedrock.heavy_model);
  return (
    <div className="card wide-card">
      <div className="panel-title">AI</div>
      <div className="pref">
        <span>Brain</span>
        <div className="seg">
          <button className={gemini ? "on" : ""} disabled={busy} onClick={() => pick("gemini", "Using Gemini.")}>
            Gemini (free)
          </button>
          {ai.bedrock && (
            <button
              className={bedrock ? "on" : ""}
              disabled={busy}
              onClick={() => pick("bedrock", "Using Claude on Bedrock. This uses your AWS credits.")}
            >
              Claude on Bedrock (paid)
            </button>
          )}
          <button
            className={provider === "ollama" ? "on" : ""}
            disabled={busy}
            onClick={() => pick("ollama", "Using the local model through Ollama.")}
          >
            Local (Ollama, heavy)
          </button>
        </div>
      </div>
      <div className="kv">
        <span>Status</span>
        <b className={ai.status === "ready" ? "tone-ok" : "tone-alert"}>{ai.status === "ready" ? "READY" : ai.status}</b>
      </div>
      {bedrock && (
        <>
          <p className="small tone-alert">
            Not free: every request that isn't an everyday command is billed to your AWS account (your credits first).
          </p>
          <form
            onSubmit={(e: FormEvent) => {
              e.preventDefault();
              if (bKey.trim()) save({ bedrock_key: bKey.trim() }, "Bedrock key saved, encrypted on this Mac.");
            }}
          >
            <label className="field-label" htmlFor="ai-bedrock-key">
              Bedrock API key {bedrock.key ? `· saved (${bedrock.key})` : "· not set"}
            </label>
            <div className="row">
              <input
                id="ai-bedrock-key"
                className="field"
                type="password"
                autoComplete="off"
                spellCheck={false}
                placeholder={bedrock.key ? "Paste a new key to replace it" : "Paste a key from the Bedrock console (API keys)"}
                value={bKey}
                maxLength={4000}
                onChange={(e) => setBKey(e.target.value)}
              />
              <button className="btn primary" disabled={busy || !bKey.trim()}>
                SAVE KEY
              </button>
            </div>
          </form>
          <div className="row-btns">
            <button className="btn ghost" onClick={() => openUrl(bedrock.key_page)}>
              GET A KEY
            </button>
            <button className="btn ghost" onClick={() => openUrl(bedrock.access_page)}>
              MODEL ACCESS
            </button>
            <button className="btn ghost" disabled={busy || !bedrock.key} onClick={test}>
              TEST
            </button>
            {bedrock.key && (
              <button className="btn ghost" disabled={busy} onClick={() => save({ bedrock_key: "" }, "Bedrock key removed.")}>
                REMOVE KEY
              </button>
            )}
          </div>
          <datalist id="bedrock-models">
            {bedrock.models.map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
          <label className="field-label" htmlFor="ai-bedrock-region">
            AWS region
          </label>
          <input id="ai-bedrock-region" className="field" value={bRegion} spellCheck={false} onChange={(e) => setBRegion(e.target.value.trim())} />
          <label className="field-label" htmlFor="ai-bedrock-fast">
            Model for most requests (commands, questions, reading and summarising mail; Haiku is cheapest)
          </label>
          <input
            id="ai-bedrock-fast"
            className="field"
            list="bedrock-models"
            value={bFast}
            spellCheck={false}
            onChange={(e) => setBFast(e.target.value.trim())}
          />
          <label className="field-label" htmlFor="ai-bedrock-heavy">
            Model for harder requests (drafting mails, writing, planning, long or multi-step; Sonnet or higher)
          </label>
          <input
            id="ai-bedrock-heavy"
            className="field"
            list="bedrock-models"
            value={bHeavy}
            spellCheck={false}
            onChange={(e) => setBHeavy(e.target.value.trim())}
          />
          {bChanged && (
            <button
              className="btn primary card-btn"
              disabled={busy}
              onClick={() =>
                save({ bedrock_region: bRegion, bedrock_fast_model: bFast, bedrock_heavy_model: bHeavy }, "Bedrock models saved.")
              }
            >
              SAVE MODELS
            </button>
          )}
        </>
      )}
      {(gemini || bedrock) && (
        <>
          <form
            onSubmit={(e: FormEvent) => {
              e.preventDefault();
              if (key.trim()) save({ api_key: key.trim() }, "Key saved, encrypted on this Mac.");
            }}
          >
            <label className="field-label" htmlFor="ai-key">
              Gemini API key{bedrock ? " (answers when Bedrock can't)" : ""} {ai.key ? `· saved (${ai.key})` : "· not set"}
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
          {gemini && (
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
          )}
        </>
      )}
      {gemini && (
        <>
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
          : bedrock
            ? "Everyday commands (volume, brightness, timers, music, screenshots) never use the AI and cost nothing. Everything else goes to Claude on Amazon Bedrock and is billed to your AWS account: JARVIS picks the model per request (most requests to the first; drafting, writing, planning, long threads and multi-step requests to the second), and you can set each to any Claude model your account has access to (turn models on under Model access). If Bedrock can't answer (throttled, no access, key expired) and a Gemini key is saved, Gemini answers instead. Keys are encrypted with your Mac's Keychain key and never shown again."
            : "The local model runs on this Mac through Ollama and uses several GB of memory while it answers."}
      </p>
    </div>
  );
}
