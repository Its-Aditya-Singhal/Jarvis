// jsdom lacks canvas, ResizeObserver and matchMedia; the UI only draws with them.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(cleanup);

const noop = () => {};
const ctx = new Proxy({}, { get: (_t, key) => (key === "createRadialGradient" || key === "createLinearGradient" ? () => ({ addColorStop: noop }) : noop), set: () => true });
HTMLCanvasElement.prototype.getContext = (() => ctx) as unknown as HTMLCanvasElement["getContext"];

class RO {
  observe = noop;
  unobserve = noop;
  disconnect = noop;
}
(globalThis as unknown as { ResizeObserver: typeof RO }).ResizeObserver ??= RO;

window.matchMedia ??= ((query: string) => ({
  matches: false, media: query, onchange: null,
  addListener: noop, removeListener: noop, addEventListener: noop, removeEventListener: noop, dispatchEvent: () => false,
})) as unknown as typeof window.matchMedia;
