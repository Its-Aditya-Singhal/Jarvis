import { ReactNode, useEffect, useState } from "react";
import AiCard from "../components/AiCard";
import AppleCard from "../components/AppleCard";
import GoogleCard from "../components/GoogleCard";
import FilesCard from "../components/FilesCard";
import FusionCard from "../components/FusionCard";
import MicCard from "../components/MicCard";
import ModelDownloads from "../components/ModelDownloads";
import VoiceEnroll from "../components/VoiceEnroll";
import VoicePicker from "../components/VoicePicker";
import { ApiError, PrefInfo, SettingsState, Status, VoiceGender, api, post } from "../lib/backend";
import { refreshStatus, setStatus, useStore } from "../lib/store";

const errText = (e: unknown) => (e instanceof ApiError ? e.message : "Backend unreachable");

const MODE_ROWS: [string, string, string, string][] = [
  ["Thinking model", "3B (fast model)", "your model", "your model"],
  ["Speech recognition", "Whisper small", "Whisper small", "Whisper medium"],
  ["Face checks / s", "4", "6", "8"],
  ["Memory suggestions", "off", "on", "on"],
];

/** A preference as a row of buttons (every setting is a fixed choice). */
function PrefControl({
  p,
  label,
  onSet,
  busy,
}: {
  p: PrefInfo | undefined;
  label: string;
  onSet: (key: string, value: PrefInfo["value"]) => void;
  busy: boolean;
}) {
  if (!p) return null;
  return (
    <div className="pref">
      <span>{label}</span>
      <div className="seg">
        {p.choices.map((c, i) => (
          <button
            key={String(c)}
            className={c === p.value ? "on" : ""}
            disabled={busy}
            onClick={() => c !== p.value && onSet(p.key, c)}
          >
            {p.labels[i]}
          </button>
        ))}
      </div>
    </div>
  );
}

export default function SettingsPanel() {
  const { auth, status } = useStore();
  const approved = auth?.state === "approved";
  const [data, setData] = useState<SettingsState | null>(null);
  const [error, setError] = useState<string | null>(null);
  // kept apart from action errors: the 5 s refresh clears its own error, never an action's
  const [loadError, setLoadError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () =>
    api<SettingsState>("/api/settings")
      .then((d) => {
        setData(d);
        setLoadError(null);
      })
      .catch((e) => setLoadError(errText(e)));
  useEffect(() => {
    if (approved) load();
    else setData(null);
  }, [approved]);
  // mode / battery / resource use change underneath us
  useEffect(() => {
    if (!approved) return;
    const id = window.setInterval(load, 5000);
    return () => window.clearInterval(id);
  }, [approved]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      await fn();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
      load();
    }
  };
  const setPref = (key: string, value: PrefInfo["value"]) =>
    run(async () => {
      const r = await api<SettingsState | { pending: string; reply: string }>("/api/settings/pref", {
        method: "PUT",
        body: JSON.stringify({ key, value }),
      });
      if ("pending" in r) setNote("That loosens security — confirm it in the card below (level 3).");
      refreshStatus();
    });

  if (!approved) {
    return (
      <div className="view">
        <h2 className="view-title">SETTINGS</h2>
        <div className="card locked-card">Owner verification required to change settings.</div>
      </div>
    );
  }
  const pref = (k: string) => data?.prefs.find((p) => p.key === k);
  const faceOn = status?.face_auth !== false;
  const ctl = (k: string, label: string) => <PrefControl p={pref(k)} label={label} onSet={setPref} busy={busy} />;

  return (
    <div className="view">
      <h2 className="view-title">SETTINGS</h2>
      {(error ?? loadError) && <p className="error small">{error ?? loadError}</p>}
      {note && <p className="note small">{note}</p>}
      {!data && !error && !loadError && <p className="muted small">Loading…</p>}
      <div className="cards" hidden={!data}>
        <IdentityCard status={status} run={run} />
        <AiCard />
        <GoogleCard />
        <div className="card">
          <div className="panel-title">VOICE</div>
          <VoicePicker
            value={status?.voice_gender ?? "female"}
            onChange={(g: VoiceGender) =>
              run(async () =>
                setStatus(await api<Status>("/api/settings/voice", { method: "PUT", body: JSON.stringify({ gender: g }) })),
              )
            }
          />
          {ctl("voice.speed", "Speaking speed")}
          {ctl("voice.ack", "When you say my name")}
          {ctl("voice.followup_s", "Keep listening after a reply for")}
          <p className="muted small">
            Speech: {status?.models.stt === "ready" ? `Whisper ${data?.mode.stt ?? ""}` : status?.models.stt} · Voice:{" "}
            {status?.models.tts === "ready" ? "Kokoro-82M" : status?.models.tts} · all on this Mac.
          </p>
        </div>

        <MicCard />

        <div className="card">
          <div className="panel-title">SECURITY</div>
          {faceOn && ctl("security.face", "Face match")}
          {ctl("security.voice", "Voice match")}
          {faceOn && ctl("security.camera", "Camera")}
          {ctl("security.typed", "Commands")}
          {ctl("security.scripts", "Anything else (generated AppleScript)")}
          {faceOn && ctl("security.away_lock_s", "Lock when I step away (camera on)")}
          {faceOn && ctl("security.liveness", "Random liveness checks")}
          <p className="muted small">
            Only safe presets are offered. Tightening needs level 2; loosening also needs a level-3 confirmation.
          </p>
        </div>

        <PerformanceCard data={data} ctl={ctl} />
        <ModelsCard data={data} run={run} />
        <div className="card">
          <div className="panel-title">ON-DEVICE MODELS</div>
          <ModelDownloads />
        </div>

        <div className="card">
          <div className="panel-title">MEMORY</div>
          {ctl("memory.enabled", "Memory")}
          {ctl("memory.suggestions", "Suggest things to remember")}
          <p className="muted small">
            Paused: nothing is logged or recalled, but “remember that…” still works. History retention and the list of
            facts are in the Memory view.
          </p>
        </div>

        {faceOn && <FusionCard />}
        <FilesCard />
        <AppleCard />
      </div>
    </div>
  );
}

function IdentityCard({ status, run }: { status: Status | null; run: (fn: () => Promise<unknown>) => void }) {
  const [owner, setOwner] = useState(status?.owner_name ?? "");
  const [assistant, setAssistant] = useState(status?.assistant_name ?? "");
  const [voiceOpen, setVoiceOpen] = useState(false);
  useEffect(() => {
    setOwner(status?.owner_name ?? "");
    setAssistant(status?.assistant_name ?? "");
  }, [status?.owner_name, status?.assistant_name]);
  const changed = owner.trim() !== status?.owner_name || assistant.trim() !== status?.assistant_name;
  return (
    <div className="card">
      <div className="panel-title">IDENTITY</div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          run(async () =>
            setStatus(
              await api<Status>("/api/settings/profile", {
                method: "PUT",
                body: JSON.stringify({ owner_name: owner, assistant_name: assistant }),
              }),
            ),
          );
        }}
      >
        <label className="field-label">Your name</label>
        <input className="field" value={owner} maxLength={40} onChange={(e) => setOwner(e.target.value)} />
        <label className="field-label">Assistant's name · also the wake word</label>
        <input className="field" value={assistant} maxLength={24} onChange={(e) => setAssistant(e.target.value)} />
        {changed && <button className="btn primary card-btn">SAVE NAMES</button>}
      </form>
      <div className="row-btns">
        {status?.face_auth !== false && (
          <button className="btn ghost" onClick={() => run(() => post("/api/privacy/reenroll_face"))}>
            RE-SCAN FACE
          </button>
        )}
        <button className="btn ghost" onClick={() => setVoiceOpen(true)}>
          {status?.voice_enrolled ? "RE-ENROLL VOICE" : "ENROLL VOICE"}
        </button>
      </div>
      <p className="muted small">
        {status?.face_auth !== false
          ? "Name changes need level 2. A face re-scan needs a level-3 confirmation; your current profile stays until the new scan is saved."
          : "Name changes need level 2: say my name first. Re-enrolling replaces your voiceprint once the new one is saved."}
      </p>
      {voiceOpen && (
        <div className="overlay">
          <div className="overlay-card">
            <VoiceEnroll
              title={status?.voice_enrolled ? "Re-enroll your voice." : "Let's learn your voice."}
              onDone={() => setVoiceOpen(false)}
              onClose={() => setVoiceOpen(false)}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function PerformanceCard({
  data,
  ctl,
}: {
  data: SettingsState | null;
  ctl: (k: string, label: string) => ReactNode;
}) {
  const m = data?.mode;
  const col = { fast: 1, balanced: 2, quality: 3 }[m?.effective ?? "balanced"];
  return (
    <div className="card wide-card">
      <div className="panel-title">PERFORMANCE</div>
      {ctl("perf.mode", "Mode")}
      <div className="kv">
        <span>Running now</span>
        <b className="tone-ok">
          {(m?.effective ?? "—").toUpperCase()}
          {m?.pref === "auto" && ` · auto (${m.on_battery ? "on battery" : "plugged in"})`}
        </b>
      </div>
      {m?.battery_pct != null && (
        <div className="kv">
          <span>Battery</span>
          <b>
            {m.battery_pct}% {m.on_battery ? "· on battery" : "· charging / plugged in"}
          </b>
        </div>
      )}
      <div className="kv">
        <span>Resource use</span>
        <b>
          CPU {m?.stats.cpu ?? "—"}% · backend {m?.stats.backend_mb ?? "—"} MB · loaded models{" "}
          {m?.stats.ollama_mb != null ? `${(m.stats.ollama_mb / 1024).toFixed(1)} GB` : "—"} · Mac memory{" "}
          {m?.stats.system_mem_pct ?? "—"}% used
        </b>
      </div>
      <table className="rates modes">
        <thead>
          <tr>
            <th />
            <th className={col === 1 ? "on" : ""}>FAST</th>
            <th className={col === 2 ? "on" : ""}>BALANCED</th>
            <th className={col === 3 ? "on" : ""}>QUALITY</th>
          </tr>
        </thead>
        <tbody>
          {MODE_ROWS.map((r) => (
            <tr key={r[0]}>
              {r.map((c, i) => (
                <td key={i} className={i === col ? "on" : ""}>
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {m?.note && <p className="note small">{m.note}</p>}
      <p className="muted small">
        Now using: {m?.llm ?? "—"} · Whisper {m?.stt ?? "—"} · {m?.face_fps ?? "—"} face checks/s. Easy commands (timers,
        alarms, apps, time) skip the model and are instant in every mode.
      </p>
    </div>
  );
}

function ModelsCard({ data, run }: { data: SettingsState | null; run: (fn: () => Promise<unknown>) => void }) {
  const { status } = useStore();
  const [models, setModels] = useState<string[]>([]);
  useEffect(() => {
    api<{ installed: string[] }>("/api/llm/models")
      .then((r) => setModels(r.installed.filter((m) => !m.startsWith("bge"))))
      .catch(() => setModels([]));
  }, []);
  const pick = (slot: "main" | "fast", current: string | undefined) => (
    <select
      className="field select"
      value={current ?? ""}
      disabled={!models.length}
      onChange={(e) =>
        run(async () =>
          setStatus(
            await api<Status>("/api/settings/llm", {
              method: "PUT",
              body: JSON.stringify({ model: e.target.value, slot }),
            }),
          ),
        )
      }
    >
      {current && !models.includes(current) && <option value={current}>{current} (not installed)</option>}
      {models.map((m) => (
        <option key={m} value={m}>
          {m}
        </option>
      ))}
    </select>
  );
  const st = status?.models.llm ?? "—";
  return (
    <div className="card">
      <div className="panel-title">LOCAL MODELS</div>
      <div className="kv">
        <span>Status</span>
        <b className={st === "ready" ? "tone-ok" : "tone-alert"}>{st === "ready" ? "READY" : st}</b>
      </div>
      <div className="kv">
        <span>Main model</span>
        {pick("main", data?.llm.main)}
      </div>
      <div className="kv">
        <span>Fast-mode model</span>
        {pick("fast", data?.llm.fast)}
      </div>
      <div className="kv">
        <span>Memory recall</span>
        <b>{status?.models.memory === "ready" ? "bge-m3" : status?.models.memory}</b>
      </div>
      <p className="muted small">
        Language models run through Ollama on this Mac. Download the assistant's models under On-device models, or any other with <code>ollama pull &lt;name&gt;</code>.
      </p>
    </div>
  );
}
