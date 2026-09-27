import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ModelsState } from "../lib/backend";
import { freshApp } from "../test/app";
import { withFakeBackend, type FakeBackend } from "../test/fakeBackend";

vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let App: typeof import("../App").default;

const GB = 1e9;

function models(patch: Partial<ModelsState["files"]> = {}, ollama: Partial<ModelsState["ollama"]> = {}): ModelsState {
  return {
    files: {
      state: "idle", error: null, file: null, done_bytes: 0, total_bytes: 0, speed_bps: 0, queued: [], current: null,
      packs: [
        { id: "face", title: "Face recognition", detail: "ArcFace", required: true, installed: false, size: 0.29 * GB, remaining: 0.29 * GB, partial: false },
        { id: "tts", title: "Voice", detail: "Kokoro", required: true, installed: false, size: 0.34 * GB, remaining: 0.34 * GB, partial: false },
        { id: "stt_medium", title: "Whisper medium", detail: "Quality mode", required: false, installed: false, size: 1.5 * GB, remaining: 1.5 * GB, partial: false },
      ],
      needed: ["face", "tts"], download_bytes: 0.63 * GB, needed_bytes: 0.93 * GB, free_bytes: 50 * GB,
      ...patch,
    },
    ollama: {
      installed: false, running: false, error: null,
      install: { url: "https://ollama.com/download/mac", brew: "brew install ollama" },
      models: [{ name: "qwen2.5:7b", purpose: "Understands requests", required: true, installed: false, size_gb: 4.7 }],
      pull: { model: null, state: "idle", status: "", completed: 0, total: 0, error: null },
      ready: false,
      ...ollama,
    },
  };
}

beforeEach(async () => {
  ({ fake } = await freshApp());
  App = (await import("../App")).default;
});

describe("First-run model download", () => {
  it("comes before setup and downloads what is missing", async () => {
    const user = userEvent.setup();
    let state = models();
    fake.route("GET /api/models", () => state);
    fake.route("POST /api/models/download", () => {
      state = models({ state: "downloading", done_bytes: 0.2 * GB, total_bytes: 0.63 * GB, speed_bps: 20e6, queued: ["face", "tts"], current: "face", file: "models/buffalo_l.zip" });
      return { started: true };
    });
    render(<App />);
    fake.setStatus({ models_needed: true });
    expect(await screen.findByText("First, the models.")).toBeInTheDocument();
    expect(screen.queryByText("Whisper medium")).not.toBeInTheDocument(); // optional packs wait for Settings
    expect(screen.getByText(/630 MB to download · 50.0 GB free/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "DOWNLOAD 630 MB" }));
    expect(fake.called("POST", "/api/models/download")).toHaveLength(1);
    expect(await screen.findByText("DOWNLOADING")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Model download" })).toHaveAttribute("aria-valuenow", "32");
    expect(screen.getByText("buffalo_l.zip")).toBeInTheDocument();
    expect(screen.getByText("IN PROGRESS")).toBeInTheDocument(); // only the pack downloading now
    expect(screen.getByText("QUEUED")).toBeInTheDocument();
    expect(screen.queryByText(/to download ·/)).not.toBeInTheDocument(); // the progress line has the numbers
    expect(screen.getByRole("button", { name: "PAUSE" })).toBeInTheDocument();
  });

  it("offers resume after an interruption and refuses when the disk is full", async () => {
    fake.route("GET /api/models", () =>
      models({ state: "error", error: "The download was interrupted. Press Resume.", free_bytes: 0.2 * GB }),
    );
    render(<App />);
    fake.setStatus({ models_needed: true });
    expect(await screen.findByText(/The download was interrupted/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "RESUME DOWNLOAD" })).toBeDisabled();
    expect(screen.getByText(/free some space first/)).toBeInTheDocument();
  });

  it("guides installing Ollama, then continues with a restart", async () => {
    const user = userEvent.setup();
    const done = models({ state: "done", needed: [], needed_bytes: 0, packs: models().files.packs.map((p) => ({ ...p, installed: p.required })) });
    fake.route("GET /api/models", () => done);
    fake.route("POST /api/models/restart", () => ({ ok: true }));
    render(<App />);
    fake.setStatus({ models_needed: true });
    expect(await screen.findByRole("button", { name: "GET OLLAMA" })).toBeInTheDocument();
    expect(screen.getByText(/instant commands still work/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "CONTINUE" }));
    await waitFor(() => expect(fake.called("POST", "/api/models/restart")).toHaveLength(1));
    expect(screen.getByRole("button", { name: "RESTARTING…" })).toBeDisabled();
  });

  it("pulls the language model once Ollama runs", async () => {
    const user = userEvent.setup();
    let state = models({}, { installed: true, running: true });
    fake.route("GET /api/models", () => state);
    fake.route("POST /api/models/ollama/pull", (body) => {
      expect(body).toEqual({ model: "qwen2.5:7b" });
      state = models({}, { installed: true, running: true, pull: { model: "qwen2.5:7b", state: "downloading", status: "downloading", completed: 1 * GB, total: 4 * GB, error: null } });
      return { started: true };
    });
    render(<App />);
    fake.setStatus({ models_needed: true });
    await user.click(await screen.findByRole("button", { name: "DOWNLOAD" }));
    expect(await screen.findByText("PULLING")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Downloading qwen2.5:7b" })).toHaveAttribute("aria-valuenow", "25");
  });
});
