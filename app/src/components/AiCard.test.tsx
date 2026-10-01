import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { freshApp } from "../test/app";
import { withFakeBackend, type FakeBackend } from "../test/fakeBackend";
import type { AiSettings } from "./AiCard";

vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let AiCard: typeof import("./AiCard").default;

const AI: AiSettings = {
  provider: "gemini", fast_model: "gemma-4-26b-a4b-it", heavy_model: "gemini-3.5-flash-lite", key: null,
  key_page: "https://aistudio.google.com/apikey", status: "no Gemini API key — add one in Settings → AI",
};

beforeEach(async () => {
  ({ fake } = await freshApp());
  AiCard = (await import("./AiCard")).default;
  fake.route("GET /api/settings/ai", () => AI);
});

describe("AI settings", () => {
  it("saves the key without ever showing it again", async () => {
    const user = userEvent.setup();
    fake.route("PUT /api/settings/ai", () => ({ ...AI, key: "…1234", status: "ready" }));
    render(<AiCard />);
    expect(await screen.findByText(/no Gemini API key/)).toBeInTheDocument();
    const input = screen.getByLabelText(/Gemini API key/);
    expect(input).toHaveAttribute("type", "password");
    await user.type(input, "AIzaSyTEST1234");
    await user.click(screen.getByRole("button", { name: "SAVE KEY" }));
    expect(fake.called("PUT", "/api/settings/ai")[0].body).toEqual({ api_key: "AIzaSyTEST1234" });
    expect(await screen.findByText("READY")).toBeInTheDocument();
    expect(screen.getByLabelText(/saved \(…1234\)/)).toHaveValue("");
  });

  it("changes the model names", async () => {
    const user = userEvent.setup();
    fake.route("PUT /api/settings/ai", (b) => ({ ...AI, ...(b as object) }));
    render(<AiCard />);
    const heavy = await screen.findByLabelText(/Writing model/);
    await user.clear(heavy);
    await user.type(heavy, "gemini-3.8-flash");
    await user.click(screen.getByRole("button", { name: "SAVE MODELS" }));
    expect(fake.called("PUT", "/api/settings/ai")[0].body).toEqual({
      fast_model: "gemma-4-26b-a4b-it", heavy_model: "gemini-3.8-flash",
    });
  });
});
