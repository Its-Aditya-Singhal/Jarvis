import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { freshApp } from "../test/app";
import { withFakeBackend, type FakeBackend } from "../test/fakeBackend";

vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let VoiceEnroll: typeof import("./VoiceEnroll").default;

beforeEach(async () => {
  ({ fake } = await freshApp());
  VoiceEnroll = (await import("./VoiceEnroll")).default;
  fake.setStatus({
    mic: { status: "active", error: null, device: "MacBook Pro Microphone" },
    models: { ...fake.status.models, voice: "ready" },
  });
});

describe("voice re-recording", () => {
  it("offers the Mac password when the current voiceprint can't confirm you", async () => {
    const user = userEvent.setup();
    fake.fail("POST /api/enroll/voice/start", 403,
      "say my name and anything first, so I know it's you, then try again (or use your Mac password)");
    fake.route("POST /api/enroll/voice/unlock", () => {
      fake.route("POST /api/enroll/voice/start", () => ({ ok: true }));
      return { ok: true };
    });
    render(<VoiceEnroll onDone={() => {}} />);
    await user.click(await screen.findByRole("button", { name: "START VOICE SCAN" }));
    expect(await screen.findByText(/so I know it's you/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "USE MAC PASSWORD" }));
    expect(fake.called("POST", "/api/enroll/voice/unlock")).toHaveLength(1);
    expect(await screen.findByText("VOICE ENROLLMENT")).toBeInTheDocument();
    expect(fake.called("POST", "/api/enroll/voice/start")).toHaveLength(2);
  });

  it("doesn't offer it for other refusals", async () => {
    const user = userEvent.setup();
    fake.fail("POST /api/enroll/voice/start", 403, "owner verification required");
    render(<VoiceEnroll onDone={() => {}} />);
    await user.click(await screen.findByRole("button", { name: "START VOICE SCAN" }));
    expect(await screen.findByText("owner verification required")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "USE MAC PASSWORD" })).toBeNull();
  });
});
