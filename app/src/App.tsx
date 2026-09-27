import { useEffect, useState } from "react";
import AlarmOverlay from "./components/AlarmOverlay";
import StatusBar from "./components/StatusBar";
import Main from "./pages/Main";
import FaceRescan from "./pages/FaceRescan";
import FirstRun from "./pages/FirstRun";
import Setup from "./pages/Setup";
import { backendInfo } from "./lib/backend";
import { startStore, useStore } from "./lib/store";

export default function App() {
  const { status, connected } = useStore();
  useEffect(startStore, []);
  const [bootError, setBootError] = useState<string | null>(null);
  useEffect(() => {
    backendInfo()
      .then((i) => setBootError(i.error ?? null))
      .catch(() => undefined);
  }, []);

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
          <p>{bootError ?? (connected ? "Loading profile…" : "Starting local systems…")}</p>
          {bootError && <p className="muted small">Reinstall the app, or see the troubleshooting section of the user guide.</p>}
        </div>
      ) : status.models_needed ? (
        <FirstRun />
      ) : status.setup_complete ? (
        status.face_reenroll ? <FaceRescan /> : <Main />
      ) : (
        <Setup />
      )}
      <AlarmOverlay />
    </div>
  );
}
