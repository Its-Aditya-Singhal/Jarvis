import { Profiler } from "react";
import { render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { freshApp } from "../test/app";
import { withFakeBackend, type FakeBackend } from "../test/fakeBackend";

vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let CameraPreview: typeof import("./CameraPreview").default;
let StatusBar: typeof import("./StatusBar").default;

beforeEach(async () => {
  ({ fake } = await freshApp());
  CameraPreview = (await import("./CameraPreview")).default;
  StatusBar = (await import("./StatusBar")).default;
});

it("camera frames re-render only the camera view, not the rest of the app", () => {
  let barRenders = 0;
  render(
    <>
      <Profiler id="bar" onRender={() => barRenders++}>
        <StatusBar />
      </Profiler>
      <CameraPreview />
    </>,
  );
  fake.setStatus({});
  const before = barRenders;
  for (let i = 0; i < 10; i++) fake.emit({ type: "preview", jpeg: `frame${i}`, boxes: [[0.4, 0.3, 0.6, 0.7]] });
  expect(barRenders).toBe(before);
  expect(screen.getByAltText("Camera preview")).toHaveAttribute("src", "data:image/jpeg;base64,frame9");
});

it("drops the last frame once the camera stops or the backend goes away", () => {
  render(<CameraPreview />);
  fake.setStatus({});
  fake.emit({ type: "preview", jpeg: "frame", boxes: [] });
  expect(screen.getByAltText("Camera preview")).toBeInTheDocument();

  fake.setStatus({ camera: { status: "off", error: null } });
  expect(screen.queryByAltText("Camera preview")).not.toBeInTheDocument();
  expect(screen.getByText("Camera off")).toBeInTheDocument();

  fake.setStatus({ camera: { status: "active", error: null } });
  fake.emit({ type: "preview", jpeg: "frame2", boxes: [] });
  expect(screen.getByAltText("Camera preview")).toHaveAttribute("src", "data:image/jpeg;base64,frame2");
  fake.connection(false);
  expect(screen.queryByAltText("Camera preview")).not.toBeInTheDocument();
});

it("the face-once card uses the assistant's own name", () => {
  render(<CameraPreview />);
  fake.setStatus({ assistant_name: "FRIDAY" });
  fake.emit({ type: "auth", state: "approved", reason: "", faces: 1, face_once: true });
  expect(screen.getByText(/until FRIDAY restarts/)).toBeInTheDocument();
});
