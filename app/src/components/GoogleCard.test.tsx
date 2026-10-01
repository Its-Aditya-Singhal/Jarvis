import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { freshApp } from "../test/app";
import { withFakeBackend, type FakeBackend } from "../test/fakeBackend";
import type { GoogleSettings } from "./GoogleCard";

vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));
const opened: string[] = [];
vi.mock("../lib/open", () => ({ openUrl: async (u: string) => void opened.push(u) }));

let fake: FakeBackend;
let GoogleCard: typeof import("./GoogleCard").default;

const G: GoogleSettings = {
  client_id: null, has_secret: false, services: { gmail: true, drive: true, calendar: true }, connected: false,
  email: null, granted: {}, connecting: false, error: null, console_url: "https://console.cloud.google.com/apis/credentials",
};
// made-up values, assembled so secret scanners don't mistake them for real credentials
const CID = ["123456789012", "fakeclient".repeat(3)].join("-") + ".apps.googleusercontent.com";
const SECRET = ["GOCSPX", "fake".repeat(4)].join("-");

beforeEach(async () => {
  ({ fake } = await freshApp());
  opened.length = 0;
  GoogleCard = (await import("./GoogleCard")).default;
  fake.route("GET /api/settings/google", () => G);
});

describe("Google account settings", () => {
  it("saves the client and connects in the browser", async () => {
    const user = userEvent.setup();
    const saved = { ...G, client_id: CID, has_secret: true };
    fake.route("PUT /api/settings/google", () => saved);
    fake.route("POST /api/settings/google/connect", () => ({ ...saved, connecting: true, url: "https://accounts.google.com/o/oauth2/v2/auth?x=1" }));
    render(<GoogleCard />);
    expect(await screen.findByText("NOT CONNECTED")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "CONNECT" })).toBeDisabled(); // no client yet
    await user.type(screen.getByLabelText(/OAuth client ID/), CID);
    const secret = screen.getByLabelText(/Client secret/);
    expect(secret).toHaveAttribute("type", "password");
    await user.type(secret, SECRET);
    await user.click(screen.getByRole("button", { name: "SAVE CLIENT" }));
    expect(fake.called("PUT", "/api/settings/google")[0].body).toEqual({ client_id: CID, client_secret: SECRET });
    await user.click(await screen.findByRole("button", { name: "CONNECT" }));
    expect(opened).toEqual(["https://accounts.google.com/o/oauth2/v2/auth?x=1"]);
    expect(await screen.findByText(/WAITING FOR THE BROWSER/)).toBeInTheDocument();
  });

  it("shows the account and disconnects", async () => {
    const user = userEvent.setup();
    const conn = { ...G, client_id: CID, has_secret: true, connected: true, email: "aditya@gmail.com",
      granted: { gmail: true, drive: true, calendar: true } };
    fake.route("GET /api/settings/google", () => conn);
    fake.route("POST /api/settings/google/disconnect", () => ({ ...conn, connected: false, email: null }));
    render(<GoogleCard />);
    expect(await screen.findByText(/CONNECTED · aditya@gmail.com/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Client secret/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "DISCONNECT" }));
    expect(await screen.findByText("NOT CONNECTED")).toBeInTheDocument();
  });

  it("switching a service off saves the rest", async () => {
    const user = userEvent.setup();
    fake.route("PUT /api/settings/google", (b) => ({ ...G, services: { gmail: true, drive: false, calendar: true }, ...(b as object) }));
    render(<GoogleCard />);
    await user.click(await screen.findByLabelText("Google Drive"));
    expect(fake.called("PUT", "/api/settings/google")[0].body).toEqual({ services: ["gmail", "calendar"] });
  });
});
