import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MicState, PrefInfo, SettingsState } from "../lib/backend";
import { freshApp } from "../test/app";
import { approvedAuth, makeStatus, withFakeBackend, type FakeBackend } from "../test/fakeBackend";

vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let SettingsPanel: typeof import("./SettingsPanel").default;

const pref = (key: string, value: PrefInfo["value"], choices: PrefInfo["value"][], labels: string[], security = false): PrefInfo =>
  ({ key, value, choices, labels, security });

const SETTINGS: SettingsState = {
  prefs: [
    pref("voice.speed", 1.0, [0.9, 1.0, 1.15], ["Slow", "Normal", "Fast"]),
    pref("security.face", "standard", ["relaxed", "standard", "strict"], ["Relaxed", "Standard", "Strict"], true),
    pref("perf.mode", "auto", ["auto", "fast", "balanced", "quality"], ["Auto", "Fast", "Balanced", "Quality"]),
    pref("memory.enabled", true, [true, false], ["On", "Paused"]),
  ],
  mode: {
    mode: "balanced", pref: "auto", on_battery: false, battery_pct: 80, effective: "balanced", note: "",
    llm: "qwen2.5:7b", stt: "small", face_fps: 6, suggestions: true, stats: { cpu: 3, backend_mb: 900 },
  },
  llm: { main: "qwen2.5:7b", fast: "qwen2.5:3b" },
  owner_name: "Aditya",
  assistant_name: "Friday",
  voice_gender: "female",
};

beforeEach(async () => {
  ({ fake } = await freshApp());
  SettingsPanel = (await import("./SettingsPanel")).default;
  fake.route("GET /api/settings", () => SETTINGS);
  fake.route("GET /api/llm/models", () => ({ installed: ["qwen2.5:7b", "qwen2.5:3b", "bge-m3:latest"] }));
  fake.route("GET /api/fusion", () => ({ enabled: true, source: "default", features: [], device_samples: { owner: 0, other: 0 }, min_owner_samples: 10, trained: null, training_samples: null, test: null }));
  fake.route("GET /api/settings/files", () => ({ folders: [], status: [] }));
  fake.route("GET /api/settings/apple", () => ({ calendar_sync: false, calendar: "", notes_sync: false, notes_folder: "JARVIS" }));
  fake.route("GET /api/mic", () => MIC);
});

const MIC: MicState = {
  status: "active", cause: null, error: null, detail: null, fix: null, device: "MacBook Pro Microphone", chosen: null,
  fallback: false, permission: "granted", app: "Terminal",
  devices: [{ name: "MacBook Pro Microphone", default: true }, { name: "Microsoft Teams Audio", default: false }],
};

const card = (title: string) => screen.getByText(title).closest(".card") as HTMLElement;

describe("Settings", () => {
  it("is locked until the owner is verified, and loads nothing", () => {
    render(<SettingsPanel />);
    expect(screen.getByText(/Owner verification required/)).toBeInTheDocument();
    expect(fake.called("GET", "/api/settings")).toHaveLength(0);
  });

  it("shows each preference with its current choice", async () => {
    render(<SettingsPanel />);
    fake.setStatus(makeStatus({ setup_complete: true, owner_name: "Aditya", assistant_name: "Friday", auth: approvedAuth() }));
    const security = await screen.findByText("SECURITY").then(() => card("SECURITY"));
    expect(within(security).getByRole("button", { name: "Standard" })).toHaveClass("on");
    expect(within(card("LOCAL MODELS")).getAllByRole("option").map((o) => o.textContent)).not.toContain("bge-m3:latest");
  });

  it("asks for a level-3 confirmation when a change loosens security", async () => {
    fake.route("PUT /api/settings/pref", () => ({ pending: "p1", reply: "Loosen security (face: Standard → Relaxed)?" }));
    render(<SettingsPanel />);
    fake.setStatus(makeStatus({ setup_complete: true, auth: approvedAuth() }));
    await screen.findByText("SECURITY");
    await userEvent.click(within(card("SECURITY")).getByRole("button", { name: "Relaxed" }));
    expect(fake.called("PUT", "/api/settings/pref")[0].body).toEqual({ key: "security.face", value: "relaxed" });
    expect(await screen.findByText(/confirm it in the card below/)).toBeInTheDocument();
  });

  it("explains a refused change (level 2 needed)", async () => {
    fake.fail("PUT /api/settings/pref", 403, "needs level 2: voice not verified recently");
    render(<SettingsPanel />);
    fake.setStatus(makeStatus({ setup_complete: true, auth: approvedAuth(1) }));
    await screen.findByText("SECURITY");
    await userEvent.click(within(card("SECURITY")).getByRole("button", { name: "Strict" }));
    expect(await screen.findByText("needs level 2: voice not verified recently")).toBeInTheDocument();
  });

  it("clicking the current choice sends nothing", async () => {
    render(<SettingsPanel />);
    fake.setStatus(makeStatus({ setup_complete: true, auth: approvedAuth() }));
    await screen.findByText("SECURITY");
    await userEvent.click(within(card("SECURITY")).getByRole("button", { name: "Standard" }));
    expect(fake.called("PUT", "/api/settings/pref")).toHaveLength(0);
  });

  it("saves new names only when they changed", async () => {
    fake.route("PUT /api/settings/profile", (body) => ({ ...fake.status, ...(body as object) }));
    render(<SettingsPanel />);
    fake.setStatus(makeStatus({ setup_complete: true, owner_name: "Aditya", assistant_name: "Friday", auth: approvedAuth() }));
    const identity = await screen.findByText("IDENTITY").then(() => card("IDENTITY"));
    expect(within(identity).queryByRole("button", { name: "SAVE NAMES" })).not.toBeInTheDocument();
    const [, assistant] = within(identity).getAllByRole("textbox");
    await userEvent.clear(assistant);
    await userEvent.type(assistant, "Edith");
    await userEvent.click(within(identity).getByRole("button", { name: "SAVE NAMES" }));
    expect(fake.called("PUT", "/api/settings/profile")[0].body).toEqual({ owner_name: "Aditya", assistant_name: "Edith" });
  });

  it("says why the microphone doesn't work and how to fix it", async () => {
    fake.route("GET /api/mic", () => ({
      ...MIC, status: "error", cause: "permission", error: "macOS hasn't allowed microphone access", permission: "denied",
      fix: "System Settings → Privacy & Security → Microphone → turn on Terminal, then quit and reopen Terminal.",
    }));
    render(<SettingsPanel />);
    fake.setStatus(makeStatus({ setup_complete: true, auth: approvedAuth() }));
    const mic = await screen.findByText("NOT WORKING").then(() => card("MICROPHONE"));
    expect(within(mic).getByText("macOS hasn't allowed microphone access")).toBeInTheDocument();
    expect(within(mic).getByText(/turn on Terminal/)).toBeInTheDocument();
    expect(within(mic).getByText("blocked")).toBeInTheDocument();
    await userEvent.click(within(mic).getByRole("button", { name: "RETRY MICROPHONE" }));
    expect(fake.called("POST", "/api/mic/retry")).toHaveLength(1);
  });

  it("switches to another microphone", async () => {
    fake.route("PUT /api/mic", () => ({ ok: true }));
    render(<SettingsPanel />);
    fake.setStatus(makeStatus({ setup_complete: true, auth: approvedAuth() }));
    await screen.findByText("LISTENING");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Microphone" }), "Microsoft Teams Audio");
    expect(fake.called("PUT", "/api/mic")[0].body).toEqual({ device: "Microsoft Teams Audio" });
  });
});

describe("Settings errors", () => {
  it("a failed refresh clears itself once the next one works", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let down = true;
      fake.route("GET /api/settings", () => {
        if (down) throw new Error("offline");
        return SETTINGS;
      });
      render(<SettingsPanel />);
      fake.setStatus(makeStatus({ setup_complete: true, auth: approvedAuth() }));
      expect(await screen.findByText("Backend unreachable")).toBeInTheDocument();
      down = false;
      await vi.advanceTimersByTimeAsync(5100);
      await vi.waitFor(() => expect(screen.queryByText("Backend unreachable")).not.toBeInTheDocument());
      expect(screen.getByText("SECURITY")).toBeVisible();
    } finally {
      vi.useRealTimers();
    }
  });

  it("an action's error stays up after the reload that follows it", async () => {
    const user = userEvent.setup();
    fake.fail("PUT /api/settings/pref", 403, "level 2 required");
    render(<SettingsPanel />);
    fake.setStatus(makeStatus({ setup_complete: true, auth: approvedAuth() }));
    await user.click(await screen.findByRole("button", { name: "Strict" }));
    expect(await screen.findByText("level 2 required")).toBeInTheDocument();
    await vi.waitFor(() => expect(fake.called("GET", "/api/settings").length).toBeGreaterThan(1));
    expect(screen.getByText("level 2 required")).toBeInTheDocument();
  });
});
