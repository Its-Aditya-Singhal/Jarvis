import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { freshApp } from "../test/app";
import { approvedAuth, withFakeBackend, type FakeBackend } from "../test/fakeBackend";

vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let ConfirmCard: typeof import("./ConfirmCard").default;

beforeEach(async () => {
  ({ fake } = await freshApp());
  ConfirmCard = (await import("./ConfirmCard")).default;
});

const pending = { id: "ab12", tool: "notes.delete", text: "Delete the note “Buy milk”?", expires_s: 30 };

describe("ConfirmCard", () => {
  it("shows nothing without a pending action", () => {
    fake.emit({ type: "auth", ...approvedAuth() });
    const { container } = render(<ConfirmCard />);
    expect(container).toBeEmptyDOMElement();
  });

  it("is never shown to someone who isn't the verified owner", () => {
    render(<ConfirmCard />);
    fake.emit({ type: "auth", state: "denied", reason: "", faces: 1, level: 0 });
    fake.emit({ type: "confirm", ...pending });
    expect(screen.queryByText(pending.text)).not.toBeInTheDocument();
  });

  it("labels deletions and counts down", () => {
    render(<ConfirmCard />);
    fake.emit({ type: "auth", ...approvedAuth() });
    fake.emit({ type: "confirm", ...pending });
    expect(screen.getByText(pending.text)).toBeInTheDocument();
    expect(screen.getByText(/CONFIRM DELETION/)).toBeInTheDocument();
    expect(screen.getByText(/^3[01] s$/)).toBeInTheDocument();
  });

  it("uses a plain CONFIRM label for settings and privacy actions", () => {
    render(<ConfirmCard />);
    fake.emit({ type: "auth", ...approvedAuth() });
    fake.emit({ type: "confirm", ...pending, tool: "privacy.factory_reset" });
    expect(screen.getByText(/LEVEL 3 · CONFIRM$/)).toBeInTheDocument();
  });

  it("sends the answer and shows the backend's refusal", async () => {
    fake.route("POST /api/confirm/ab12", () => ({ ok: false, reply: "Finish the liveness check first, then confirm." }));
    render(<ConfirmCard />);
    fake.emit({ type: "auth", ...approvedAuth() });
    fake.emit({ type: "confirm", ...pending });
    await userEvent.click(screen.getByRole("button", { name: "CONFIRM" }));
    expect(fake.called("POST", "/api/confirm/ab12")[0].body).toEqual({ accept: true });
    expect(await screen.findByText("Finish the liveness check first, then confirm.")).toBeInTheDocument();
  });

  it("cancels, and disappears when the backend says it is done", async () => {
    fake.route("POST /api/confirm/ab12", () => ({ ok: true, reply: "Okay, I won't delete it." }));
    render(<ConfirmCard />);
    fake.emit({ type: "auth", ...approvedAuth() });
    fake.emit({ type: "confirm", ...pending });
    await userEvent.click(screen.getByRole("button", { name: "CANCEL" }));
    expect(fake.called("POST", "/api/confirm/ab12")[0].body).toEqual({ accept: false });
    fake.emit({ type: "confirm_done", id: "ab12", outcome: "cancelled" });
    await waitFor(() => expect(screen.queryByText(pending.text)).not.toBeInTheDocument());
  });

  it("disappears when it expires", () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<ConfirmCard />);
      fake.emit({ type: "auth", ...approvedAuth() });
      fake.emit({ type: "confirm", ...pending, expires_s: 1 });
      expect(screen.getByText(pending.text)).toBeInTheDocument();
      vi.advanceTimersByTime(1500);
      fake.emit({ type: "auth", ...approvedAuth() }); // any re-render
      expect(screen.queryByText(pending.text)).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});
