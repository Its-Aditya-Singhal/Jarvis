// Single app-wide store fed by backend events.

import { useSyncExternalStore } from "react";
import {
  Activity,
  AuthPublic,
  BackendEvent,
  EnrollSnapshot,
  MemorySuggestion,
  PendingConfirm,
  PlannedAction,
  RingingAlarm,
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
  thinking: boolean;
  ringing: RingingAlarm[];
  /** bumps whenever alarms/events/notes change, so views refetch */
  toolsVersion: number;
  /** a deletion waiting for the owner's confirmation (expiresAt: epoch ms) */
  confirm: (PendingConfirm & { expiresAt: number }) | null;
  /** facts the assistant offers to remember (tap to save) */
  suggestions: MemorySuggestion[];
  /** bumps when facts or history change */
  memoryVersion: number;
  /** bumps after a privacy action (dashboard refetches) */
  privacyVersion: number;
}

export interface Turn {
  who: "you" | "assistant";
  text: string;
  at: number;
  actions?: PlannedAction[];
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
  thinking: false,
  ringing: [],
  toolsVersion: 0,
  confirm: null,
  suggestions: [],
  memoryVersion: 0,
  privacyVersion: 0,
};

const withExpiry = (p: PendingConfirm | null | undefined) =>
  p ? { ...p, expiresAt: Date.now() + p.expires_s * 1000 } : null;

let turnSeq = 0;
const addTurn = (who: Turn["who"], text: string, actions?: PlannedAction[]) =>
  // at + seq keeps keys unique when two turns land in the same millisecond
  set({ conversation: [...state.conversation, { who, text, at: Date.now() + ++turnSeq / 1000, actions }].slice(-6) });

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
      set({
        status,
        auth: status.auth,
        ringing: status.ringing ?? [],
        confirm: withExpiry(status.pending),
        suggestions: status.suggestions ?? [],
      });
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
      addTurn("assistant", e.text, e.actions);
      break;
    case "thinking":
      set({ thinking: e.active });
      break;
    case "alarm": {
      const { type: _t, count: _c, ...a } = e;
      if (!state.ringing.some((r) => r.id === a.id)) set({ ringing: [...state.ringing, a] });
      break;
    }
    case "alarm_stopped":
      set({ ringing: [], toolsVersion: state.toolsVersion + 1 });
      break;
    case "tools_changed":
      set({ toolsVersion: state.toolsVersion + 1 });
      break;
    case "confirm": {
      const { type: _t, ...p } = e;
      set({ confirm: withExpiry(p) });
      break;
    }
    case "memory_suggestion":
      if (!state.suggestions.some((s) => s.id === e.id))
        set({ suggestions: [...state.suggestions, { id: e.id, text: e.text }].slice(-4) });
      break;
    case "memory_changed":
      set({ memoryVersion: state.memoryVersion + 1 });
      break;
    case "privacy_changed":
      set({ privacyVersion: state.privacyVersion + 1, memoryVersion: state.memoryVersion + 1, toolsVersion: state.toolsVersion + 1 });
      refreshStatus();
      break;
    case "face_reenroll":
      set({ enroll: null, enrollComplete: false });
      refreshStatus();
      break;
    case "confirm_done":
      if (state.confirm?.id === e.id) set({ confirm: null });
      break;
    case "listening":
      set({ listeningUntil: e.active ? Date.now() + (e.seconds ?? 8) * 1000 : 0 });
      break;
  }
}

export async function refreshStatus() {
  try {
    const status = await api<Status>("/api/status");
    // keep the local countdown unless the pending item changed
    const confirm = status.pending?.id === state.confirm?.id ? state.confirm : withExpiry(status.pending);
    set({ status, auth: status.auth, ringing: status.ringing ?? [], confirm, suggestions: status.suggestions ?? [] });
  } catch {
    /* backend still starting; the websocket will deliver status */
  }
}

export function resetFaceEnroll() {
  set({ enroll: null, enrollComplete: false });
}

export function resetVoiceEnroll() {
  set({ voiceEnroll: null, voiceEnrollComplete: false, voiceEnrollCancelled: null });
}

export function dropSuggestion(id: string) {
  set({ suggestions: state.suggestions.filter((s) => s.id !== id) });
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
