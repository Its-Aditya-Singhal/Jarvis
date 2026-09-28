import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { freshApp } from "../test/app";
import { withFakeBackend, type FakeBackend } from "../test/fakeBackend";

vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let ActiveTimers: typeof import("./ActiveTimers").default;

beforeEach(async () => {
  ({ fake } = await freshApp());
  ActiveTimers = (await import("./ActiveTimers")).default;
});

const iso = (msFromNow: number) => new Date(Date.now() + msFromNow).toISOString();

describe("ActiveTimers", () => {
  it("shows running timers and delayed actions with a countdown on the main screen", async () => {
    fake.route("GET /api/tools", () => ({
      alarms: [
        { id: 1, kind: "timer", due: iso(65_000), label: "", status: "pending" },
        { id: 2, kind: "timer", due: iso(10_000), label: "", status: "done" },
        { id: 3, kind: "alarm", due: iso(3_600_000), label: "", status: "pending" },
      ],
      delayed: [{ id: "ab", due: iso(30_000), summary: "closing WhatsApp" }],
    }));
    render(<ActiveTimers />);
    expect(await screen.findByText("closing WhatsApp")).toBeInTheDocument();
    expect(screen.getByText("TIMER")).toBeInTheDocument();
    expect(screen.getByText(/^1:0[45]$/)).toBeInTheDocument();
    expect(screen.getByText(/^0:[23][09]$/)).toBeInTheDocument();
    expect(screen.getAllByRole("button")).toHaveLength(2); // the finished timer and the alarm aren't shown
  });

  it("cancels a delayed action", async () => {
    fake.route("GET /api/tools", () => ({ alarms: [], delayed: [{ id: "ab", due: iso(60_000), summary: "closing Slack" }] }));
    fake.route("POST /api/delayed/ab/cancel", () => ({ ok: true }));
    render(<ActiveTimers />);
    await userEvent.click(await screen.findByRole("button", { name: "Cancel closing Slack" }));
    expect(fake.called("POST", "/api/delayed/ab/cancel")).toHaveLength(1);
    await waitFor(() => expect(screen.queryByText("closing Slack")).not.toBeInTheDocument());
  });

  it("shows nothing when nothing is running", async () => {
    fake.route("GET /api/tools", () => ({ alarms: [], delayed: [] }));
    const { container } = render(<ActiveTimers />);
    await waitFor(() => expect(fake.called("GET", "/api/tools")).toHaveLength(1));
    expect(container).toBeEmptyDOMElement();
  });
});
