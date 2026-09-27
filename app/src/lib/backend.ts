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
  | { type: "level"; level: number }
  | { type: "speaking"; active: boolean }
  | { type: "voice"; verdict: VoiceState }
  | ({ type: "voice_enroll" } & VoiceEnrollSnapshot)
  | { type: "voice_enroll_complete" }
  | { type: "voice_enroll_cancelled"; reason: string };
