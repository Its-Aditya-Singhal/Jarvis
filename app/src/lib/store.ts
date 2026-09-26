// Single app-wide store fed by backend events.

import { useSyncExternalStore } from "react";
import {
  Activity,
  AuthPublic,
  BackendEvent,
  EnrollSnapshot,
  Status,
  api,
  connectEvents,
} from "./backend";

export interface Preview {
  src: string;
  boxes: number[][];
}

export interface AppState {
  connected: boolean;
  status: Status | null;
  auth: AuthPublic | null;
  enroll: EnrollSnapshot | null;
  enrollComplete: boolean;
  activity: Activity[];
  preview: Preview | null;
  speech: { text: string; at: number } | null;
}

let state: AppState = {
  connected: false,
  status: null,
  auth: null,
  enroll: null,
  enrollComplete: false,
  activity: [],
  preview: null,
  speech: null,
};

const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());
const set = (patch: Partial<AppState>) => {
  state = { ...state, ...patch };
  emit();
};

function handle(e: BackendEvent) {
  switch (e.type) {
    case "status": {
      const { type: _t, ...status } = e;
      set({ status, auth: status.auth });
      break;
    }
    case "auth": {
      const { type: _t, ...auth } = e;
      set({ auth, status: state.status ? { ...state.status, auth } : state.status });
      break;
    }
    case "enroll": {
      const { type: _t, ...snap } = e;
      set({ enroll: snap });
      break;
    }
    case "enroll_complete":
      set({ enrollComplete: true });
      refreshStatus();
      break;
    case "activity": {
      const { type: _t, ...a } = e;
      // de-duplicate history replayed on reconnect
      if (state.activity.some((x) => x.ts === a.ts && x.text === a.text)) return;
      set({ activity: [...state.activity, a].slice(-80) });
      break;
    }
    case "preview":
      set({ preview: { src: `data:image/jpeg;base64,${e.jpeg}`, boxes: e.boxes } });
      break;
    case "say":
      set({ speech: { text: e.text, at: Date.now() } });
      break;
  }
}

export async function refreshStatus() {
  try {
    const status = await api<Status>("/api/status");
    set({ status, auth: status.auth });
  } catch {
    /* backend still starting; the websocket will deliver status */
  }
}

export function setStatus(status: Status) {
  set({ status, auth: status.auth });
}

let started = false;
export function startStore() {
  if (started) return;
  started = true;
  connectEvents(handle, (up) => {
    set({ connected: up });
    if (up) refreshStatus();
  });
  // camera status isn't pushed as an event; poll it cheaply
  window.setInterval(() => state.connected && refreshStatus(), 3000);
}

export function useStore(): AppState {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => state,
  );
}
