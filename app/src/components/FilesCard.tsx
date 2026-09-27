import { useEffect, useState } from "react";
import { ApiError, api } from "../lib/backend";

const inTauri = "__TAURI_INTERNALS__" in window;

/** Folders the assistant may search with Spotlight (owner only). */
export default function FilesCard() {
  const [folders, setFolders] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<{ folders: string[] }>("/api/settings/files")
      .then((r) => setFolders(r.folders))
      .catch((e) => setError(e instanceof ApiError ? e.message : "unavailable"));
  }, []);

  const save = async (next: string[]) => {
    setError(null);
    try {
      const r = await api<{ folders: string[] }>("/api/settings/files", {
        method: "PUT",
        body: JSON.stringify({ folders: next }),
      });
      setFolders(r.folders);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Backend unreachable");
    }
  };

  const add = async () => {
    let picked: string | null = null;
    if (inTauri) {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const r = await open({ directory: true, multiple: false, title: "Allow file search in…" });
      picked = typeof r === "string" ? r : null;
    } else {
      picked = window.prompt("Folder path to allow (e.g. ~/Projects)");
    }
    if (picked) save([...folders, picked]);
  };

  const home = (p: string) => p.replace(/^\/Users\/[^/]+/, "~");
  return (
    <div className="card">
      <div className="panel-title">FILE SEARCH ACCESS</div>
      {folders.map((f) => (
        <div className="kv item" key={f}>
          <span className="mono">{home(f)}</span>
          <button className="btn ghost mini" onClick={() => save(folders.filter((x) => x !== f))}>
            REMOVE
          </button>
        </div>
      ))}
      {folders.length === 0 && <p className="muted small">No folders allowed — file search is off.</p>}
      <button className="btn ghost card-btn" onClick={add}>
        + ADD FOLDER
      </button>
      {error && <p className="error small">{error}</p>}
      <p className="muted small">
        Searches use Spotlight and return file names only. System, library and hidden folders can't be added.
      </p>
    </div>
  );
}
