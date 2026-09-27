import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { freshApp } from "../test/app";
import { makeStatus, withFakeBackend, type FakeBackend } from "../test/fakeBackend";

vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let Setup: typeof import("./Setup").default;

beforeEach(async () => {
  ({ fake } = await freshApp());
  Setup = (await import("./Setup")).default;
});

const PHRASES = [
  { lang: "English", text: "Hello Friday, authenticate me." },
  { lang: "Hindi", text: "Friday, kal subah saat baje mujhe jagana." },
];

describe("Setup wizard", () => {
  it("walks from welcome through names, face and voice to done", async () => {
    const user = userEvent.setup();
    fake.route("POST /api/setup/profile", (body) => {
      const b = body as { owner_name: string; assistant_name: string };
      fake.status = makeStatus({ owner_name: b.owner_name, assistant_name: b.assistant_name });
      return fake.status;
    });
    fake.route("POST /api/enroll/face/start", () => ({ ok: true }));
    fake.route("POST /api/enroll/voice/start", () => ({ ok: true }));
    fake.route("POST /api/setup/complete", () => ({ ...fake.status, setup_complete: true }));
    render(<Setup />);

    await user.click(screen.getByRole("button", { name: "BEGIN SETUP" }));
    const [owner, assistant] = screen.getAllByRole("textbox");
    expect(assistant).toHaveValue("JARVIS");
    await user.type(owner, "Aditya");
    await user.clear(assistant);
    await user.type(assistant, "Friday");
    await user.click(screen.getByRole("radio", { name: /^MALE/ }));
    expect(screen.getByRole("radio", { name: /^MALE/ })).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("button", { name: "CONTINUE" }));
    const profile = fake.called("POST", "/api/setup/profile")[0].body as Record<string, string>;
    expect(profile.owner_name).toBe("Aditya");
    expect(profile.assistant_name).toBe("Friday");
    expect(profile.voice_gender).toBe("male");

    // face scan: prompts and progress come from the backend
    await user.click(await screen.findByRole("button", { name: "START FACE SCAN" }));
    fake.emit({
      type: "enroll", step: "left", prompt: "Turn your head slightly left", hint: "Turn a little further",
      step_index: 1, step_count: 9, progress: 0.25, done: false, steps: ["straight", "left", "right"],
    });
    expect(screen.getByText("Turn your head slightly left")).toBeInTheDocument();
    expect(screen.getByText("Turn a little further")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "25");
    fake.status = { ...fake.status, face_enrolled: true };
    fake.emit({ type: "enroll_complete" });
    expect(await screen.findByText("Face profile captured.")).toBeInTheDocument();

    // voice scan
    await user.click(await screen.findByRole("button", { name: "START VOICE SCAN" }, { timeout: 3000 }));
    fake.emit({
      type: "voice_enroll", index: 0, count: 2, text: PHRASES[0].text, lang: "English", hint: "",
      progress: 0, done: false, quality: null, phrases: PHRASES,
    });
    expect(screen.getByText(`“${PHRASES[0].text}”`)).toBeInTheDocument();
    fake.status = { ...fake.status, voice_enrolled: true };
    fake.emit({ type: "voice_enroll_complete" });

    // done
    await user.click(await screen.findByRole("button", { name: "ENTER FRIDAY" }, { timeout: 3000 }));
    expect(fake.called("POST", "/api/setup/complete")).toHaveLength(1);
    expect(screen.getByText("Voice enrolled")).toBeInTheDocument();
  });

  it("shows why the profile was refused", async () => {
    const user = userEvent.setup();
    fake.fail("POST /api/setup/profile", 409, "setup already completed");
    render(<Setup />);
    await user.click(screen.getByRole("button", { name: "BEGIN SETUP" }));
    await user.type(screen.getAllByRole("textbox")[0], "Mallory");
    await user.click(screen.getByRole("button", { name: "CONTINUE" }));
    expect(await screen.findByText("setup already completed")).toBeInTheDocument();
  });

  it("won't continue with a blank name", async () => {
    const user = userEvent.setup();
    render(<Setup />);
    await user.click(screen.getByRole("button", { name: "BEGIN SETUP" }));
    await user.type(screen.getAllByRole("textbox")[0], "   ");
    expect(screen.getByRole("button", { name: "CONTINUE" })).toBeDisabled();
  });

  it("waits for the camera before a face scan can start", async () => {
    fake.status = makeStatus({ owner_name: "Aditya", camera: { status: "error", error: "Camera unavailable" } });
    await act(async () => {
      await (await import("../lib/store")).refreshStatus();
    });
    render(<Setup />);
    expect(screen.getByRole("button", { name: "WAITING FOR CAMERA…" })).toBeDisabled();
    fake.setStatus({ camera: { status: "active", error: null } });
    await waitFor(() => expect(screen.getByRole("button", { name: "START FACE SCAN" })).toBeEnabled());
  });

  it("resumes at the voice step and offers a skip when voice can't work", async () => {
    fake.status = makeStatus({
      owner_name: "Aditya", face_enrolled: true,
      mic: { status: "error", error: "Microphone unavailable. Check System Settings.", device: null },
    });
    await act(async () => {
      await (await import("../lib/store")).refreshStatus();
    });
    render(<Setup />);
    expect(screen.getByText("Microphone unavailable. Check System Settings.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "WAITING FOR MICROPHONE…" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: /SKIP — VOICE UNAVAILABLE/ }));
    expect(screen.getByText("Voice skipped — no working microphone")).toBeInTheDocument();
  });
});
