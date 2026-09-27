// Single app-wide store fed by backend events.

import { useSyncExternalStore } from "react";
import {
  Activity,
  AuthPublic,
  BackendEvent,
  EnrollSnapshot,
  Status,
  VoiceEnrollSnapshot,
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
  speaking: boolean;
  voiceEnroll: VoiceEnrollSnapshot | null;
  voiceEnrollComplete: boolean;
  voiceEnrollCancelled: string | null;
  assistantSpeaking: boolean;
  /** owner-addressed turns only; unaddressed speech never reaches the UI */
  conversation: Turn[];
  listeningUntil: number;
}

export interface Turn {
  who: "you" | "assistant";
  text: string;
  at: number;
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
  speaking: false,
  voiceEnroll: null,
  voiceEnrollComplete: false,
  voiceEnrollCancelled: null,
  assistantSpeaking: false,
  conversation: [],
  listeningUntil: 0,
};

const addTurn = (who: Turn["who"], text: string) =>
  set({ conversation: [...state.conversation, { who, text, at: Date.now() }].slice(-6) });

// The microphone level arrives ~15x/second. It lives outside React state so
// animations can read it every frame without re-rendering the app.
let micLevel = 0;
export const getMicLevel = () => micLevel;

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
    case "level":
      micLevel = e.level;
      break;
    case "speaking":
      set({ speaking: e.active });
      break;
    case "voice":
      refreshStatus();
      break;
    case "voice_enroll": {
      const { type: _t, ...snap } = e;
      set({ voiceEnroll: snap, voiceEnrollCancelled: null });
      break;
    }
    case "voice_enroll_complete":
      set({ voiceEnrollComplete: true });
      refreshStatus();
      break;
    case "voice_enroll_cancelled":
      set({ voiceEnroll: null, voiceEnrollCancelled: e.reason });
      break;
    case "tts":
      set(e.active && e.text ? { assistantSpeaking: true, speech: { text: e.text, at: Date.now() } } : { assistantSpeaking: e.active });
      break;
    case "heard":
      addTurn("you", e.text);
      break;
    case "reply":
      addTurn("assistant", e.text);
      break;
    case "listening":
      set({ listeningUntil: e.active ? Date.now() + (e.seconds ?? 8) * 1000 : 0 });
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

export function resetVoiceEnroll() {
  set({ voiceEnroll: null, voiceEnrollComplete: false, voiceEnrollCancelled: null });
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
