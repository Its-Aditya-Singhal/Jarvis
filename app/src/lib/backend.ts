// Connection to the local Python backend.
// Inside the desktop app the shell supplies a random port + per-launch token.
// For UI development in a plain browser, VITE_BACKEND_PORT points at a
// manually started backend (no token).

import { invoke } from "@tauri-apps/api/core";

export interface BackendInfo {
  port: number;
  token: string;
}

const inTauri = "__TAURI_INTERNALS__" in window;
let infoPromise: Promise<BackendInfo> | null = null;

export function backendInfo(): Promise<BackendInfo> {
  if (!infoPromise) {
    infoPromise = inTauri
      ? invoke<BackendInfo>("backend_info")
      : Promise.resolve({ port: Number(import.meta.env.VITE_BACKEND_PORT ?? 8765), token: "" });
  }
  return infoPromise;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const { port, token } = await backendInfo();
  const res = await fetch(`http://127.0.0.1:${port}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export const post = <T = unknown>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

/** WebSocket with automatic reconnect. Returns a disposer. */
export function connectEvents(
  onEvent: (e: BackendEvent) => void,
  onConnection: (up: boolean) => void,
): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let retry: number | undefined;

  const open = async () => {
    const { port, token } = await backendInfo();
    if (closed) return;
    ws = new WebSocket(`ws://127.0.0.1:${port}/ws${token ? `?token=${token}` : ""}`);
    ws.onopen = () => onConnection(true);
    ws.onmessage = (m) => onEvent(JSON.parse(m.data));
    ws.onclose = () => {
      onConnection(false);
      if (!closed) retry = window.setTimeout(open, 1000);
    };
  };
  open();
  return () => {
    closed = true;
    window.clearTimeout(retry);
    ws?.close();
  };
}

// ---- event / status types ------------------------------------------------

// "liveness": the face matched but a live person hasn't been confirmed yet
// "spoof": the matched face looks like a photo/screen, or the camera feed is frozen
export type AuthState = "no_profile" | "scanning" | "liveness" | "approved" | "denied" | "absent" | "spoof";

export interface AuthPublic {
  state: AuthState;
  reason: string;
  faces: number;
  face_confidence?: number | null; // only present while the owner is verified
  bystander?: boolean;
  voice?: VoicePublic;
  liveness?: LivenessPublic;
  /** auth level 0-2 from live evidence (level 3 is granted per confirmed action) */
  level?: number;
  trust?: TrustPublic; // owner-only
}

export interface TrustPublic {
  level: number;
  name: string;
  prob: number | null; // fusion classifier on all evidence: P(the live owner is in control)
  presence: number | null; // the same without voice evidence (gates level 1)
  blockers: Record<string, string>; // level -> why it isn't reached
  l3_ready: boolean;
  voice_window_s: number | null;
  features: Record<string, number>;
  model: string; // default | personal | disabled | error text
}

export interface PendingConfirm {
  id: string;
  tool: string;
  text: string;
  expires_s: number;
}

export interface FusionInfo {
  enabled: boolean;
  source: string;
  trained: string | null;
  training_samples: { simulated: number; device_owner: number; device_other: number } | null;
  test: {
    n: number;
    accuracy: number;
    auc: number;
    level1: { threshold: number; far: number; frr: number };
    level2: { threshold: number; far: number; frr: number };
    level3: { threshold: number; far: number; frr: number };
  } | null;
  device_samples: { owner: number; other: number };
  min_owner_samples: number;
  features: string[];
}

export type LivenessState = "idle" | "challenge" | "cooldown" | "passed" | "spoof" | "disabled";

export interface Challenge {
  step: string;
  prompt: string;
  hint: string;
  step_index: number;
  step_count: number;
  steps: string[];
  remaining_s: number;
}

export interface LivenessPublic {
  state: LivenessState;
  reason: string;
  challenge?: Challenge;
  cooldown_s?: number;
  live_score?: number | null; // owner-only
  checked_ago?: number | null; // owner-only
  next_check_s?: number | null; // owner-only
}

export type VoiceState = "idle" | "verified" | "uncertain" | "rejected";

export interface VoicePublic {
  state: VoiceState;
  confidence?: number; // owner-only
  seconds_ago?: number; // owner-only
}

export type VoiceGender = "female" | "male";

export interface Status {
  setup_complete: boolean;
  owner_name: string;
  assistant_name: string;
  face_enrolled: boolean;
  voice_enrolled: boolean;
  mode: "idle" | "enrolling" | "verifying";
  voice_mode: "idle" | "enrolling" | "verifying" | "unavailable";
  camera: { status: "off" | "starting" | "active" | "error"; error: string | null };
  mic: { status: "off" | "starting" | "active" | "error"; error: string | null; device: string | null };
  models: Record<string, string>;
  auth: AuthPublic;
  voice_gender: VoiceGender;
  listening: boolean;
  llm_model: string | null;
  ringing: RingingAlarm[];
  pending: PendingConfirm | null; // owner-only
  suggestions: MemorySuggestion[]; // owner-only
  issues: Issue[];
  perf: PerfStatus;
  network: { offline: boolean; external: number; blocked: number };
  /** face profile missing or a re-scan authorised: show the scan screen */
  face_reenroll: { allowed: boolean; reason: string; locked_out: boolean; kind: "deleted" | "redo" | null } | null;
}

export interface Issue {
  id: string;
  level: "error" | "warn";
  title: string;
  fix: string;
}

export type PerfMode = "fast" | "balanced" | "quality";

export interface PerfStatus {
  mode: PerfMode;
  pref: "auto" | PerfMode;
  on_battery: boolean;
  battery_pct: number | null;
  cpu?: number;
  backend_mb?: number;
  ollama_mb?: number;
}

export interface PrefInfo {
  key: string;
  value: string | number | boolean;
  choices: (string | number | boolean)[];
  labels: string[];
  security: boolean;
}

export interface SettingsState {
  prefs: PrefInfo[];
  mode: PerfStatus & {
    effective: PerfMode;
    note: string;
    llm: string | null;
    stt: string | null;
    face_fps: number;
    suggestions: boolean;
    stats: { cpu?: number; backend_mb?: number; ollama_mb?: number; system_mem_pct?: number };
  };
  llm: { main: string; fast: string };
  owner_name: string;
  assistant_name: string;
  voice_gender: VoiceGender;
}

export interface PrivacyItem {
  id: string;
  title: string;
  present: boolean;
  detail: string;
  updated: string | null;
  protection: string;
  action: string | null;
}

export interface NetworkInfo {
  offline: boolean;
  external: { process: string; host: string; port: number; status: string; count: number }[];
  attempts: { ts: number; host: string; port: number | null; what: string; blocked: boolean }[];
  blocked: number;
}

export interface PrivacyState {
  items: PrivacyItem[];
  never_stored: string[];
  location: string;
  keychain_key: boolean;
  db_bytes: number;
  network: NetworkInfo;
}

export interface MemorySuggestion {
  id: string;
  text: string;
}

export interface RingingAlarm {
  id: number;
  kind: "alarm" | "timer";
  label: string;
  due: string;
}

export interface PlannedAction {
  tool: string;
  args: Record<string, unknown>;
  summary: string;
  ok?: boolean; // present once the tool has run
  result?: string;
  data?: { files?: string[]; pending?: string; blocked?: string; [k: string]: unknown };
}

export interface CommandResult {
  reply: string;
  language: string;
  actions: PlannedAction[];
  ok: boolean;
  latency_s?: number;
}

export interface EnrollSnapshot {
  step: string;
  prompt: string;
  hint: string;
  step_index: number;
  step_count: number;
  progress: number;
  done: boolean;
  steps: string[];
}

export interface VoiceEnrollSnapshot {
  index: number;
  count: number;
  text: string;
  lang: string;
  hint: string;
  progress: number;
  done: boolean;
  quality: number | null;
  phrases: { lang: string; text: string }[];
  accepted?: boolean;
}

export interface Activity {
  ts: number;
  text: string;
  level: "info" | "ok" | "warn" | "alert" | "error";
}

export type BackendEvent =
  | ({ type: "status" } & Status)
  | ({ type: "auth" } & AuthPublic)
  | ({ type: "enroll" } & EnrollSnapshot)
  | { type: "enroll_complete" }
  | ({ type: "activity" } & Activity)
  | { type: "preview"; jpeg: string; boxes: number[][] }
  | { type: "say"; text: string }
  | { type: "level"; level: number; source?: "assistant" }
  | { type: "tts"; active: boolean; text?: string }
  | { type: "heard"; text: string; lang: "en" | "hi"; stt_s?: number; source?: "voice" | "typed" }
  | { type: "reply"; text: string; actions?: PlannedAction[] }
  | { type: "thinking"; active: boolean }
  | ({ type: "alarm"; count: number } & RingingAlarm)
  | { type: "alarm_stopped" }
  | { type: "tools_changed" }
  | ({ type: "confirm" } & PendingConfirm)
  | { type: "confirm_done"; id: string; outcome: "done" | "cancelled" | "expired" }
  | ({ type: "memory_suggestion" } & MemorySuggestion)
  | { type: "memory_changed" }
  | { type: "privacy_changed" }
  | { type: "face_reenroll"; kind: string }
  | { type: "listening"; active: boolean; seconds?: number }
  | { type: "speaking"; active: boolean }
  | { type: "voice"; verdict: VoiceState }
  | ({ type: "voice_enroll" } & VoiceEnrollSnapshot)
  | { type: "voice_enroll_complete" }
  | { type: "voice_enroll_cancelled"; reason: string };
