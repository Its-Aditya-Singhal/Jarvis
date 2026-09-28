import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { freshApp } from "../test/app";
import { approvedAuth, makeStatus, withFakeBackend, type FakeBackend } from "../test/fakeBackend";

vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let MemoryPanel: typeof import("./MemoryPanel").default;

const MEMORY = {
  facts: [{ id: 1, text: "Riya is my sister", source: "said", created: "2026-09-01T10:00", updated: "2026-09-01T10:00" }],
  suggestions: [],
  status: { facts: 1, turns: 2, retention: "30d", recall: "meaning (bge-m3)" },
  retention_choices: ["off", "7d", "30d", "forever"],
};

const turn = (you: string) => ({ time: "2026-09-27T09:00", you, reply: null });

beforeEach(async () => {
  ({ fake } = await freshApp());
  MemoryPanel = (await import("./MemoryPanel")).default;
  fake.route("GET /api/memory", () => MEMORY);
});

it("a slow answer to an older search doesn't replace the newer results", async () => {
  const user = userEvent.setup();
  const pending: Record<string, (v: unknown) => void> = {};
  fake.route("GET /api/history", (_b, path) => {
    const q = new URL(path, "http://x").searchParams.get("q") ?? "";
    return q === "ri" ? new Promise((resolve) => (pending[q] = resolve)) : { turns: [turn(`about ${q || "everything"}`)] };
  });
  render(<MemoryPanel />);
  fake.setStatus(makeStatus({ setup_complete: true, auth: approvedAuth() }));
  const search = await screen.findByPlaceholderText("Search what we talked about…");
  await user.type(search, "ri");
  await vi.waitFor(() => expect(pending.ri).toBeDefined());
  await user.type(search, "ya");
  expect(await screen.findByText("about riya")).toBeInTheDocument();
  pending.ri({ turns: [turn("about ri")] });
  await new Promise((r) => setTimeout(r, 20));
  expect(screen.queryByText("about ri")).not.toBeInTheDocument();
  expect(screen.getByText("about riya")).toBeInTheDocument();
});

it("a failed load clears once memory loads again", async () => {
  let down = true;
  fake.route("GET /api/memory", () => {
    if (down) throw new Error("offline");
    return MEMORY;
  });
  fake.route("GET /api/history", () => ({ turns: [] }));
  render(<MemoryPanel />);
  fake.setStatus(makeStatus({ setup_complete: true, auth: approvedAuth() }));
  expect(await screen.findByText("Backend unreachable")).toBeInTheDocument();
  down = false;
  fake.emit({ type: "memory_changed" });
  expect(await screen.findByText("Riya is my sister")).toBeInTheDocument();
  expect(screen.queryByText("Backend unreachable")).not.toBeInTheDocument();
});
