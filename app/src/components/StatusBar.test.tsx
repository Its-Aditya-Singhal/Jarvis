import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AuthState } from "../lib/backend";
import { freshApp } from "../test/app";
import { approvedAuth, withFakeBackend, type FakeBackend } from "../test/fakeBackend";

vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let StatusBar: typeof import("./StatusBar").default;

beforeEach(async () => {
  ({ fake } = await freshApp());
  StatusBar = (await import("./StatusBar")).default;
});

const value = (label: string) => {
  const ind = screen.getByText(label).closest(".indicator") as HTMLElement;
  return within(ind).getByText((_, el) => !!el?.classList.contains("indicator-value"));
};

describe("StatusBar", () => {
  it("shows the assistant's name and the core connection", async () => {
    render(<StatusBar />);
    fake.setStatus({ assistant_name: "Friday" });
    expect(screen.getByText("FRIDAY")).toBeInTheDocument();
    expect(value("CORE")).toHaveTextContent("ONLINE");
  });

  it.each<[AuthState, string]>([
    ["denied", "INTRUDER"],
    ["spoof", "SPOOF"],
    ["liveness", "LIVENESS"],
    ["approved", "PROTECTED"],
    ["absent", "LOCKED"],
  ])("security reads %s as %s", (state, label) => {
    render(<StatusBar />);
    fake.setStatus({ face_enrolled: true, auth: { state, reason: "", faces: 1, level: 0 } });
    expect(value("SECURITY")).toHaveTextContent(label);
  });

  it("flags a blocked camera and microphone", () => {
    render(<StatusBar />);
    fake.setStatus({
      camera: { status: "error", error: "Camera unavailable" },
      mic: { status: "error", error: "Microphone unavailable", device: null },
    });
    expect(value("CAMERA")).toHaveTextContent("BLOCKED");
    expect(value("MIC")).toHaveTextContent("BLOCKED");
  });

  it("shows the network guard and the auth level", () => {
    render(<StatusBar />);
    fake.setStatus({ network: { offline: true, external: 0, blocked: 2 }, auth: approvedAuth(2) });
    expect(value("NETWORK")).toHaveTextContent("OFFLINE ✓");
    expect(value("LEVEL")).toHaveTextContent("L2 ACT");
    fake.setStatus({ network: { offline: false, external: 3, blocked: 0 } });
    expect(value("NETWORK")).toHaveTextContent("3 OUT");
  });

  it("shows speaking, thinking and listening", () => {
    render(<StatusBar />);
    fake.setStatus({});
    fake.emit({ type: "thinking", active: true });
    expect(value("LOCAL AI")).toHaveTextContent("THINKING");
    fake.emit({ type: "tts", active: true, text: "Hello" });
    expect(value("SPEECH")).toHaveTextContent("SPEAKING");
    fake.emit({ type: "listening", active: true, seconds: 8 });
    expect(value("MIC")).toHaveTextContent("LISTENING");
  });
});
