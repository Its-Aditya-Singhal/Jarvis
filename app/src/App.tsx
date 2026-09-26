import { useEffect } from "react";
import StatusBar from "./components/StatusBar";
import Main from "./pages/Main";
import Setup from "./pages/Setup";
import { startStore, useStore } from "./lib/store";

export default function App() {
  const { status, connected } = useStore();
  useEffect(startStore, []);

  const name = status?.assistant_name;
  useEffect(() => {
    if (!name) return;
    document.title = name;
    if ("__TAURI_INTERNALS__" in window) {
      import("@tauri-apps/api/window").then(({ getCurrentWindow }) => getCurrentWindow().setTitle(name));
    }
  }, [name]);

  return (
    <div className="app">
      <div className="bg-grid" />
      <StatusBar />
      {!status ? (
        <div className="boot">
          <div className="boot-ring" />
          <p>{connected ? "Loading profile…" : "Starting local systems…"}</p>
        </div>
      ) : status.setup_complete ? (
        <Main />
      ) : (
        <Setup />
      )}
    </div>
  );
}
