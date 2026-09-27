import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PrefInfo, SettingsState } from "../lib/backend";
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
});

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
});
