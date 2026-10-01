import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { freshApp } from "../test/app";
import { withFakeBackend, type FakeBackend } from "../test/fakeBackend";

vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let MacUnlock: typeof import("./MacUnlock").default;

beforeEach(async () => {
  ({ fake } = await freshApp());
  MacUnlock = (await import("./MacUnlock")).default;
});

describe("Mac password unlock", () => {
  it("is offered in voice-only Settings below level 2 and counts down once unlocked", async () => {
    const user = userEvent.setup();
    fake.setStatus({ face_auth: false, setup_complete: true, assistant_name: "JARVIS",
      auth: { ...fake.status.auth, level: 1, mac_unlock_s: 0 } });
    fake.route("POST /api/unlock", () => ({ ok: true, seconds: 120 }));
    render(<MacUnlock />);
    expect(await screen.findByText(/say “JARVIS, hello” first/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "USE MAC PASSWORD" }));
    expect(fake.called("POST", "/api/unlock")).toHaveLength(1);
    expect(await screen.findByText(/120 s left/)).toBeInTheDocument();
  });

  it("stays hidden once the voice gives level 2", async () => {
    fake.setStatus({ face_auth: false, setup_complete: true, auth: { ...fake.status.auth, level: 2 } });
    const { container } = render(<MacUnlock />);
    expect(container).toBeEmptyDOMElement();
  });
});
