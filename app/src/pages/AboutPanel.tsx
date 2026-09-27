import { openUrl } from "../lib/open";
import { useStore } from "../lib/store";

const LINKS: [string, string][] = [
  ["Website", "https://its-aditya-singhal.github.io/Jarvis/"],
  ["User guide", "https://github.com/Its-Aditya-Singhal/Jarvis/blob/main/docs/USER_GUIDE.md"],
  ["Security and its limits", "https://github.com/Its-Aditya-Singhal/Jarvis/blob/main/docs/SECURITY.md"],
  ["Source code", "https://github.com/Its-Aditya-Singhal/Jarvis"],
  ["Report a problem", "https://github.com/Its-Aditya-Singhal/Jarvis/issues"],
];

const MODELS: [string, string, string][] = [
  ["face", "Face", "InsightFace SCRFD + ArcFace (buffalo_l)"],
  ["liveness", "Anti-spoofing", "InsightFace liveness CNN + challenges"],
  ["voice", "Voice", "ECAPA-TDNN (SpeechBrain) + Silero VAD"],
  ["stt", "Speech recognition", "Whisper (MLX on the GPU, else faster-whisper)"],
  ["tts", "Speech", "Kokoro-82M"],
  ["llm", "Language", "Ollama"],
  ["memory", "Memory recall", "bge-m3 via Ollama"],
];

export const SHORTCUTS: [string, string][] = [
  ["⌘K", "Command palette"],
  ["⌘1 … ⌘7", "Switch views"],
  ["⌘,", "Settings"],
  ["⌘I", "About"],
  ["/", "Type a command"],
  ["Esc", "Close the palette"],
];

export default function AboutPanel() {
  const { status } = useStore();
  const name = status?.assistant_name ?? "JARVIS";
  return (
    <div className="view">
      <h2 className="view-title">ABOUT</h2>
      <div className="cards">
        <div className="card about-card">
          <div className="panel-title">{name.toUpperCase()}</div>
          <p className="about-version">
            Version <b>{status?.version ?? "—"}</b>
          </p>
          <p className="muted small">
            A local-first assistant that keeps checking who is in front of the Mac. Every model runs here; nothing is
            sent to a cloud. Built on JARVIS, free and open source.
          </p>
          <div className="about-links">
            {LINKS.map(([label, url]) => (
              <button key={url} className="btn ghost small-btn" onClick={() => openUrl(url)}>
                {label.toUpperCase()}
              </button>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="panel-title">MODELS ON THIS MAC</div>
          {MODELS.map(([key, label, what]) => {
            const st = status?.models[key] ?? "—";
            const ok = st === "ready";
            return (
              <div className="kv" key={key}>
                <span>
                  {label} <em className="muted small">· {what}</em>
                </span>
                <b className={ok ? "tone-ok" : "tone-warn"}>{key === "llm" && ok ? (status?.llm_model ?? "READY") : ok ? "READY" : st}</b>
              </div>
            );
          })}
          <p className="muted small">
            Model licences: InsightFace models are for non-commercial research use; the others are permissive.
          </p>
        </div>
        <div className="card">
          <div className="panel-title">KEYBOARD</div>
          {SHORTCUTS.map(([k, what]) => (
            <div className="kv" key={k}>
              <span>{what}</span>
              <kbd>{k}</kbd>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
