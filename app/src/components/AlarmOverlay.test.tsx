import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { freshApp } from "../test/app";
import { withFakeBackend, type FakeBackend } from "../test/fakeBackend";

vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let AlarmOverlay: typeof import("./AlarmOverlay").default;

beforeEach(async () => {
  ({ fake } = await freshApp());
  AlarmOverlay = (await import("./AlarmOverlay")).default;
});

const ring = () => fake.emit({ type: "alarm", id: 3, kind: "alarm", label: "Gym", due: "2026-09-28T07:00", count: 0 });

it("anyone can dismiss or snooze a ringing alarm", async () => {
  fake.route("POST /api/alarms/dismiss", () => ({ dismissed: 1 }));
  fake.route("POST /api/alarms/snooze", () => ({ snoozed: 1 }));
  render(<AlarmOverlay />);
  ring();
  expect(screen.getByRole("alertdialog", { name: "Alarm" })).toHaveTextContent("Gym");
  await userEvent.click(screen.getByRole("button", { name: "SNOOZE 5 MIN" }));
  expect(fake.called("POST", "/api/alarms/snooze")[0].body).toEqual({ minutes: 5 });
  await userEvent.click(screen.getByRole("button", { name: "DISMISS" }));
  fake.emit({ type: "alarm_stopped" });
  expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
});

it("says so when the alarm can't be silenced from here", async () => {
  fake.fail("POST /api/alarms/dismiss", 503, "tools disabled");
  render(<AlarmOverlay />);
  ring();
  await userEvent.click(screen.getByRole("button", { name: "DISMISS" }));
  expect(await screen.findByText("tools disabled")).toBeInTheDocument();
});
