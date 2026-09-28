// A scripted backend for UI tests: canned REST routes and pushed events.
//
//   vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));
//   const { fake } = await freshApp();          // new store + fake per test
//   fake.route("GET /api/settings", () => settingsFixture);
//   fake.emit({ type: "auth", ...approvedAuth });

import { act } from "@testing-library/react";
import type { AuthPublic, BackendEvent, Status } from "../lib/backend";

type Backend = typeof import("../lib/backend");
type Handler = (body: unknown, path: string) => unknown;

export interface Call {
  method: string;
  path: string;
  body: unknown;
}

export class FakeBackend {
  routes = new Map<string, Handler>();
  calls: Call[] = [];
  status: Status = makeStatus();
  private onEvent: ((e: BackendEvent) => void) | null = null;
  private onConnection: ((up: boolean) => void) | null = null;

  constructor(private real: Backend) {
    this.reset();
  }

  /** Forget routes, calls and status (the mocked module outlives each test). */
  reset() {
    this.routes.clear();
    this.calls = [];
    this.status = makeStatus();
    this.onEvent = null;
    this.onConnection = null;
    this.route("GET /api/status", () => this.status);
  }

  route(key: string, handler: Handler) {
    this.routes.set(key, handler);
  }

  /** A route that answers with an HTTP error. */
  fail(key: string, status: number, detail: string) {
    this.route(key, () => {
      throw new this.real.ApiError(status, detail);
    });
  }

  called(method: string, path: string) {
    return this.calls.filter((c) => c.method === method && c.path === path);
  }

  emit(e: BackendEvent) {
    act(() => this.onEvent?.(e));
  }

  /** The websocket dropping (backend quit or crashed) or coming back. */
  connection(up: boolean) {
    act(() => this.onConnection?.(up));
  }

  /** Push a status update, the way the backend does on connect. */
  setStatus(patch: Partial<Status>) {
    this.status = { ...this.status, ...patch };
    this.emit({ type: "status", ...this.status });
  }

  api = async (path: string, init: RequestInit = {}) => {
    const method = (init.method ?? "GET").toUpperCase();
    const body = init.body ? JSON.parse(String(init.body)) : undefined;
    this.calls.push({ method, path, body });
    const handler = this.routes.get(`${method} ${path}`) ?? this.routes.get(`${method} ${path.split("?")[0]}`);
    if (!handler) throw new this.real.ApiError(404, `no fake route for ${method} ${path}`);
    return handler(body, path);
  };

  connectEvents = (onEvent: (e: BackendEvent) => void, onConnection: (up: boolean) => void) => {
    this.onEvent = onEvent;
    this.onConnection = onConnection;
    onConnection(true);
    return () => {
      this.onEvent = null;
      this.onConnection = null;
    };
  };
}

export function withFakeBackend(real: Backend) {
  const fake = new FakeBackend(real);
  return {
    ...real,
    fake,
    backendInfo: async () => ({ port: 0, token: "" }),
    api: fake.api,
    post: (path: string, body?: unknown) =>
      fake.api(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
    connectEvents: fake.connectEvents,
  };
}

export const approvedAuth = (level = 2): AuthPublic => ({
  state: "approved",
  reason: "Owner verified",
  faces: 1,
  level,
  face_confidence: 0.93,
  liveness: { state: "passed", reason: "Live person confirmed" },
  voice: { state: "verified" },
});

export function makeStatus(patch: Partial<Status> = {}): Status {
  return {
    setup_complete: false,
    owner_name: "",
    assistant_name: "JARVIS",
    face_enrolled: false,
    voice_enrolled: false,
    mode: "idle",
    voice_mode: "idle",
    camera: { status: "active", error: null },
    mic: { status: "active", error: null, device: "MacBook Pro Microphone" },
    models: { face: "ready", voice: "ready", liveness: "ready", llm: "ready", tools: "ready", memory: "ready", stt: "ready", tts: "ready" },
    auth: { state: "no_profile", reason: "", faces: 0, level: 0 },
    voice_gender: "female",
    listening: false,
    llm_model: "qwen2.5:7b",
    ringing: [],
    pending: null,
    suggestions: [],
    issues: [],
    perf: { mode: "balanced", pref: "auto", on_battery: false, battery_pct: null },
    network: { offline: true, external: 0, blocked: 0 },
    face_reenroll: null,
    ...patch,
  };
}
