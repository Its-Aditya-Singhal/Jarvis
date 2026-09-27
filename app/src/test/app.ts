import { vi } from "vitest";
import type { FakeBackend } from "./fakeBackend";

/** Fresh store and fake backend for one test (the store is a module singleton). */
export async function freshApp(): Promise<{ fake: FakeBackend; store: typeof import("../lib/store") }> {
  vi.resetModules();
  const backend = (await import("../lib/backend")) as unknown as { fake: FakeBackend };
  backend.fake.reset();
  const store = await import("../lib/store");
  store.startStore();
  return { fake: backend.fake, store };
}
