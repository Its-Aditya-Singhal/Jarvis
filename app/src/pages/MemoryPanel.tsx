import { useEffect, useState } from "react";
import { ApiError, api, post } from "../lib/backend";
import { useStore } from "../lib/store";

interface Fact {
  id: number;
  text: string;
  source: "said" | "suggested" | "typed";
  created: string;
  updated: string;
}

interface MemoryState {
  facts: Fact[];
  suggestions: { id: string; text: string }[];
  status: { facts: number; turns: number; retention: string; recall: string };
  retention_choices: string[];
}

interface HistoryTurn {
  time: string;
  you: string;
  reply: string | null;
}

const RETENTION_LABEL: Record<string, string> = { off: "Off", "7d": "7 days", "30d": "30 days", forever: "Forever" };
const SOURCE_LABEL: Record<string, string> = { said: "said", suggested: "suggested", typed: "typed" };
const fmt = (iso: string) =>
  new Date(iso).toLocaleString([], {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "numeric",
    minute: "2-digit",
  });

const errText = (e: unknown) => (e instanceof ApiError ? e.message : "Backend unreachable");

/** Remembered facts, suggestions and conversation history (verified owner only). */
export default function MemoryPanel() {
  const { auth, memoryVersion } = useStore();
  const approved = auth?.state === "approved";
  const [data, setData] = useState<MemoryState | null>(null);
  const [turns, setTurns] = useState<HistoryTurn[] | null>(null);
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState<{ id: number; text: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  // kept apart from action errors, and cleared by the next successful load
  const [loadError, setLoadError] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);

  const load = () => {
    api<MemoryState>("/api/memory")
      .then((d) => {
        setData(d);
        setLoadError(null);
      })
      .catch((e) => setLoadError(errText(e)));
  };
  useEffect(() => {
    if (!approved) {
      setData(null);
      setTurns(null);
      return;
    }
    load();
  }, [approved, memoryVersion]);

  useEffect(() => {
    if (!approved) return;
    let live = true; // a slower answer to an older search must not replace a newer one
    const id = window.setTimeout(() => {
      api<{ turns: HistoryTurn[] }>(`/api/history?q=${encodeURIComponent(query)}`)
        .then((r) => {
          if (!live) return;
          setTurns(r.turns);
          setLoadError(null);
        })
        .catch((e) => live && setLoadError(errText(e)));
    }, 250);
    return () => {
      live = false;
      window.clearTimeout(id);
    };
  }, [approved, query, memoryVersion]);

  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      load();
    } catch (e) {
      setError(errText(e));
    }
  };

  if (!approved) {
    return (
      <div className="view">
        <h2 className="view-title">MEMORY</h2>
        <div className="card locked-card">Owner verification required to view memory.</div>
      </div>
    );
  }

  return (
    <div className="view">
      <h2 className="view-title">MEMORY</h2>
      {(error ?? loadError) && <p className="error small">{error ?? loadError}</p>}
      <div className="cards">
        <div className="card memory-card">
          <div className="panel-title">REMEMBERED · {data?.facts.length ?? 0}</div>
          <form
            className="memory-add"
            onSubmit={(e) => {
              e.preventDefault();
              const t = draft.trim();
              if (!t) return;
              setDraft("");
              act(() => post("/api/memory", { text: t }));
            }}
          >
            <input
              className="field"
              value={draft}
              maxLength={300}
              placeholder="Add something to remember…"
              onChange={(e) => setDraft(e.target.value)}
            />
          </form>
          {data?.suggestions.map((s) => (
            <div key={s.id} className="fact suggestion">
              <span>
                <em>Suggested</em> {s.text}
              </span>
              <button
                className="mini ok"
                onClick={() => act(() => post(`/api/memory/suggestions/${s.id}`, { accept: true }))}
              >
                SAVE
              </button>
              <button
                className="mini"
                onClick={() => act(() => post(`/api/memory/suggestions/${s.id}`, { accept: false }))}
              >
                ✕
              </button>
            </div>
          ))}
          {data?.facts.length === 0 && (
            <p className="muted small">
              Nothing yet. Say “{"remember that…"}” or type above. Facts are encrypted on this Mac.
            </p>
          )}
          {data?.facts.map((f) =>
            editing?.id === f.id ? (
              <form
                key={f.id}
                className="fact"
                onSubmit={(e) => {
                  e.preventDefault();
                  const t = editing.text.trim();
                  setEditing(null);
                  if (t && t !== f.text)
                    act(() => api(`/api/memory/${f.id}`, { method: "PUT", body: JSON.stringify({ text: t }) }));
                }}
              >
                <input
                  className="field"
                  autoFocus
                  value={editing.text}
                  maxLength={300}
                  onChange={(e) => setEditing({ id: f.id, text: e.target.value })}
                  onBlur={() => setEditing(null)}
                />
              </form>
            ) : (
              <div key={f.id} className="fact">
                <span title={`${SOURCE_LABEL[f.source]} · ${fmt(f.updated)}`}>{f.text}</span>
                <button className="mini" onClick={() => setEditing({ id: f.id, text: f.text })}>
                  EDIT
                </button>
                <button
                  className="mini danger"
                  onClick={() => act(() => api(`/api/memory/${f.id}`, { method: "DELETE" }))}
                >
                  FORGET
                </button>
              </div>
            ),
          )}
          <p className="muted small">Recall: {data?.status.recall ?? "—"}</p>
        </div>

        <div className="card memory-card">
          <div className="panel-title">CONVERSATION HISTORY</div>
          <div className="history-bar">
            <input
              className="field"
              value={query}
              placeholder="Search what we talked about…"
              onChange={(e) => setQuery(e.target.value)}
            />
            <select
              className="field select"
              value={data?.status.retention ?? "30d"}
              title="How long history is kept"
              onChange={(e) =>
                act(() =>
                  api("/api/settings/history", { method: "PUT", body: JSON.stringify({ retention: e.target.value }) }),
                )
              }
            >
              {(data?.retention_choices ?? []).map((r) => (
                <option key={r} value={r}>
                  Keep: {RETENTION_LABEL[r] ?? r}
                </option>
              ))}
            </select>
          </div>
          <div className="history">
            {turns?.length === 0 && (
              <p className="muted small">
                {query
                  ? "No matches."
                  : data?.status.retention === "off"
                    ? "History is turned off."
                    : "No conversations yet."}
              </p>
            )}
            {turns?.map((t, i) => (
              <div key={t.time + i} className="history-turn">
                <time>{fmt(t.time)}</time>
                <div>
                  <b>YOU</b> {t.you}
                </div>
                {t.reply && (
                  <div className="muted">
                    <b>↳</b> {t.reply}
                  </div>
                )}
              </div>
            ))}
          </div>
          <div className="row-btns">
            {confirmClear ? (
              <>
                <button
                  className="btn danger"
                  onClick={() => {
                    setConfirmClear(false);
                    act(() => post("/api/privacy/clear_history")); // level 3: confirm in the card below
                  }}
                >
                  DELETE ALL HISTORY
                </button>
                <button className="btn ghost" onClick={() => setConfirmClear(false)}>
                  KEEP
                </button>
              </>
            ) : (
              <button className="btn ghost" disabled={!data?.status.turns} onClick={() => setConfirmClear(true)}>
                CLEAR HISTORY
              </button>
            )}
          </div>
          <p className="muted small">
            Only what you say to the assistant and its replies, as text. Audio is never stored; everything is encrypted.
          </p>
        </div>
      </div>
    </div>
  );
}
