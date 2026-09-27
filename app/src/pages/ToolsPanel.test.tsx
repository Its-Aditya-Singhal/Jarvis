import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { freshApp } from "../test/app";
import { approvedAuth, makeStatus, withFakeBackend, type FakeBackend } from "../test/fakeBackend";

vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let ToolsPanel: typeof import("./ToolsPanel").default;

beforeEach(async () => {
  ({ fake } = await freshApp());
  ToolsPanel = (await import("./ToolsPanel")).default;
  fake.route("GET /api/tools", () => ({
    alarms: [{ id: 7, kind: "alarm", due: "2026-09-28T07:00:00", label: "", status: "pending" }],
    events: [],
    notes: [],
  }));
});

it("explains why cancelling an alarm was refused", async () => {
  fake.fail("POST /api/alarms/7/cancel", 403, "needs level 2: voice not verified recently");
  render(<ToolsPanel />);
  fake.setStatus(makeStatus({ setup_complete: true, auth: approvedAuth(1) }));
  await userEvent.click(await screen.findByRole("button", { name: "CANCEL" }));
  expect(await screen.findByText("needs level 2: voice not verified recently")).toBeInTheDocument();
});
