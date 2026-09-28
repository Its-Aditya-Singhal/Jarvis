import { act, renderHook } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { freshApp } from "../test/app";
import { withFakeBackend, type FakeBackend } from "../test/fakeBackend";

vi.mock("./backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let store: typeof import("./store");

beforeEach(async () => {
  ({ fake, store } = await freshApp());
});

const snapshot = () => renderHook(() => store.useStore()).result.current;

it("a lost connection clears thinking, speaking, listening and the last camera frame", () => {
  fake.emit({ type: "thinking", active: true });
  fake.emit({ type: "tts", active: true, text: "Opening Safari" });
  fake.emit({ type: "speaking", active: true });
  fake.emit({ type: "listening", active: true, seconds: 8 });
  fake.emit({ type: "preview", jpeg: "frame", boxes: [] });
  const before = snapshot();
  expect(before.thinking && before.assistantSpeaking && before.speaking).toBe(true);

  fake.connection(false);
  const after = snapshot();
  expect(after.connected).toBe(false);
  expect(after.thinking).toBe(false);
  expect(after.assistantSpeaking).toBe(false);
  expect(after.speaking).toBe(false);
  expect(after.listeningUntil).toBe(0);
  expect(renderHook(() => store.usePreview()).result.current).toBeNull();
});

it("status pushes keep the confirmation countdown running instead of restarting it", () => {
  vi.useFakeTimers();
  try {
    const pending = { id: "c1", tool: "notes.delete", text: "Delete the note?", expires_s: 30 };
    fake.setStatus({ pending });
    const first = snapshot().confirm!.expiresAt;
    act(() => vi.advanceTimersByTime(10_000));
    fake.setStatus({ pending });
    expect(snapshot().confirm!.expiresAt).toBe(first);
    // a different confirmation gets its own countdown
    fake.setStatus({ pending: { ...pending, id: "c2" } });
    expect(snapshot().confirm!.expiresAt).toBe(first + 10_000);
  } finally {
    vi.useRealTimers();
  }
});
