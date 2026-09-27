import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PrivacyState } from "../lib/backend";
import { freshApp } from "../test/app";
import { approvedAuth, makeStatus, withFakeBackend, type FakeBackend } from "../test/fakeBackend";

vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let PrivacyPanel: typeof import("./PrivacyPanel").default;

const PRIVACY: PrivacyState = {
  items: [
    { id: "face", title: "Face profile", present: true, detail: "45 samples", updated: null, protection: "AES-256-GCM", action: "delete_face" },
    { id: "voice", title: "Voice profile", present: false, detail: "none", updated: null, protection: "AES-256-GCM", action: "delete_voice" },
    { id: "log", title: "Security log", present: true, detail: "12 events", updated: null, protection: "local only", action: "clear_security_log" },
  ],
  never_stored: ["Camera frames", "Audio recordings"],
  location: "/Users/aditya/Library/Application Support/JarvisAssistant",
  keychain_key: true,
  db_bytes: 40960,
  network: { offline: true, external: [], attempts: [{ ts: 1, host: "example.com", port: 443, what: "connect", blocked: true }], blocked: 1 },
};

beforeEach(async () => {
  ({ fake } = await freshApp());
  PrivacyPanel = (await import("./PrivacyPanel")).default;
  fake.route("GET /api/privacy", () => PRIVACY);
});

const unlock = () => fake.setStatus(makeStatus({ setup_complete: true, auth: approvedAuth() }));

describe("Privacy dashboard", () => {
  it("is locked until the owner is verified", () => {
    render(<PrivacyPanel />);
    expect(screen.getByText(/Owner verification required/)).toBeInTheDocument();
    expect(fake.called("GET", "/api/privacy")).toHaveLength(0);
  });

  it("lists what is stored, hides the home folder, and shows blocked connections", async () => {
    render(<PrivacyPanel />);
    unlock();
    expect(await screen.findByText("Face profile")).toBeInTheDocument();
    expect(screen.getByText("~/Library/Application Support/JarvisAssistant")).toBeInTheDocument();
    expect(screen.getByText("✕ Camera frames")).toBeInTheDocument();
    expect(screen.getByText("BLOCKED · OFFLINE")).toBeInTheDocument();
    expect(screen.getByText(/connect example.com:443/)).toBeInTheDocument();
  });

  it("requests deletions through a confirmation, and only for data that exists", async () => {
    fake.route("POST /api/privacy/delete_face", () => ({ ok: true, pending: "p1" }));
    render(<PrivacyPanel />);
    unlock();
    await screen.findByText("Face profile");
    const deletes = screen.getAllByRole("button", { name: "DELETE" });
    expect(deletes[1]).toBeDisabled(); // no voice profile
    await userEvent.click(deletes[0]);
    expect(fake.called("POST", "/api/privacy/delete_face")).toHaveLength(1);
    expect(await screen.findByText(/Confirm below/)).toBeInTheDocument();
  });

  it("needs ERASE typed before a factory reset can be requested", async () => {
    fake.route("POST /api/privacy/factory_reset", () => ({ ok: true, pending: "p2" }));
    render(<PrivacyPanel />);
    unlock();
    await screen.findByText("Face profile");
    const erase = screen.getByRole("button", { name: "ERASE EVERYTHING" });
    expect(erase).toBeDisabled();
    await userEvent.type(screen.getByPlaceholderText("Type ERASE"), "erase");
    expect(erase).toBeDisabled();
    await userEvent.clear(screen.getByPlaceholderText("Type ERASE"));
    await userEvent.type(screen.getByPlaceholderText("Type ERASE"), "ERASE");
    await userEvent.click(erase);
    expect(fake.called("POST", "/api/privacy/factory_reset")).toHaveLength(1);
  });

  it("allowing internet access asks for confirmation", async () => {
    fake.route("PUT /api/settings/pref", () => ({ pending: "p3", reply: "Allow internet?" }));
    render(<PrivacyPanel />);
    unlock();
    await userEvent.click(await screen.findByRole("button", { name: "ALLOW INTERNET ACCESS" }));
    expect(fake.called("PUT", "/api/settings/pref")[0].body).toEqual({ key: "privacy.offline", value: false });
    expect(await screen.findByText(/needs a level-3 confirmation/)).toBeInTheDocument();
  });

  it("shows why an action was refused", async () => {
    fake.fail("POST /api/privacy/clear_security_log", 403, "needs level 2: voice not verified recently");
    render(<PrivacyPanel />);
    unlock();
    await userEvent.click(await screen.findByRole("button", { name: "CLEAR" }));
    expect(await screen.findByText("needs level 2: voice not verified recently")).toBeInTheDocument();
  });
});
