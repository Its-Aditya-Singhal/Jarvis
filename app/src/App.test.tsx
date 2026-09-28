import { render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { freshApp } from "./test/app";
import { approvedAuth, makeStatus, withFakeBackend, type FakeBackend } from "./test/fakeBackend";

vi.mock("./lib/backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let App: typeof import("./App").default;

beforeEach(async () => {
  ({ fake } = await freshApp());
  App = (await import("./App")).default;
});

it("when the engine keeps stopping, the shell's reason replaces the stale screen", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    render(<App />);
    fake.setStatus(makeStatus({ setup_complete: true, owner_name: "Aditya", face_enrolled: true, auth: approvedAuth() }));
    expect(screen.getByRole("heading", { name: "AUTHENTICATION APPROVED" })).toBeInTheDocument();

    fake.connection(false);
    expect(await screen.findByRole("heading", { name: "CORE OFFLINE" })).toBeInTheDocument();
    fake.shellError = "The assistant's engine keeps stopping (exit status: 1). Quit and reopen the app.";
    await vi.advanceTimersByTimeAsync(2100);
    expect(await screen.findByText(/keeps stopping/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "CORE OFFLINE" })).not.toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
});

it("an engine that failed to start at launch is explained on the boot screen", async () => {
  fake.shellError = "The assistant's engine could not start: No such file or directory";
  render(<App />);
  fake.connection(false); // nothing ever answers
  expect(await screen.findByText(/could not start/)).toBeInTheDocument();
});
