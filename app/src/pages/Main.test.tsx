import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { freshApp } from "../test/app";
import { approvedAuth, makeStatus, withFakeBackend, type FakeBackend } from "../test/fakeBackend";

vi.mock("../lib/backend", async (orig) => withFakeBackend(await orig()));

let fake: FakeBackend;
let Main: typeof import("./Main").default;

beforeEach(async () => {
  ({ fake } = await freshApp());
  Main = (await import("./Main")).default;
});

describe("Keyboard and command palette", () => {
  it("⌘K opens the palette; typing filters, Enter goes, Esc closes", async () => {
    const user = userEvent.setup();
    render(<Main />);
    fake.setStatus(makeStatus({ setup_complete: true, owner_name: "Aditya", version: "1.0.0" }));
    fake.emit({ type: "auth", ...approvedAuth() });
    await user.keyboard("{Meta>}k{/Meta}");
    const dialog = screen.getByRole("dialog", { name: "Command palette" });
    await user.keyboard("abt");
    const options = within(dialog).getAllByRole("option");
    expect(options).toHaveLength(1);
    expect(options[0]).toHaveTextContent("About");
    await user.keyboard("{Enter}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "ABOUT" })).toBeInTheDocument();
    expect(screen.getByText("1.0.0")).toBeInTheDocument();

    await user.keyboard("{Meta>}k{/Meta}");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("⌘-number switches views, and the nav marks the current one", async () => {
    const user = userEvent.setup();
    render(<Main />);
    fake.setStatus(makeStatus({ setup_complete: true, owner_name: "Aditya", version: "1.0.0" }));
    fake.emit({ type: "auth", ...approvedAuth() });
    await user.keyboard("{Meta>}7{/Meta}");
    expect(screen.getByRole("heading", { name: "SETTINGS" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Settings/ })).toHaveAttribute("aria-current", "page");
    await user.keyboard("{Meta>}i{/Meta}");
    expect(screen.getByRole("heading", { name: "ABOUT" })).toBeInTheDocument();
  });

  it("/ jumps to the command bar", async () => {
    const user = userEvent.setup();
    render(<Main />);
    fake.setStatus(makeStatus({ setup_complete: true, owner_name: "Aditya", version: "1.0.0" }));
    fake.emit({ type: "auth", ...approvedAuth() });
    await user.keyboard("{Meta>}3{/Meta}");
    await user.keyboard("/");
    const input = await screen.findByRole("textbox", { name: "Command" });
    await vi.waitFor(() => expect(input).toHaveFocus());
  });
});

describe("Main screen", () => {
  const owner = () => {
    fake.setStatus(
      makeStatus({ setup_complete: true, owner_name: "Aditya", face_enrolled: true, voice_enrolled: true, auth: approvedAuth() }),
    );
  };

  it("a command that fails to send is given back, so it can be sent again", async () => {
    const user = userEvent.setup();
    fake.fail("POST /api/command", 503, "The assistant is busy");
    render(<Main />);
    owner();
    const input = screen.getByRole("textbox", { name: "Command" });
    await user.type(input, "open safari{Enter}");
    expect(await screen.findByText("The assistant is busy")).toBeInTheDocument();
    expect(input).toHaveValue("open safari");
  });

  it("while a command is being worked on, typing keeps going and Enter keeps the text", async () => {
    const user = userEvent.setup();
    fake.route("POST /api/command", () => ({ ok: true }));
    render(<Main />);
    owner();
    fake.emit({ type: "thinking", active: true });
    const input = screen.getByRole("textbox", { name: "Command" });
    expect(input).toBeEnabled();
    await user.type(input, "and close it{Enter}");
    expect(input).toHaveValue("and close it");
    expect(input).toHaveFocus();
    expect(fake.called("POST", "/api/command")).toHaveLength(0);

    fake.emit({ type: "thinking", active: false });
    await user.type(input, "{Enter}");
    expect(fake.called("POST", "/api/command")[0].body).toEqual({ text: "and close it" });
    expect(input).toHaveValue("");
  });

  it("LISTENING ends when the listening window runs out", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<Main />);
      owner();
      fake.emit({ type: "listening", active: true, seconds: 2.2 }); // the clock ticks every 0.5 s
      expect(screen.getByRole("heading", { name: "LISTENING…" })).toBeInTheDocument();
      // an unrelated re-render just after the window closes, before the next clock tick
      await vi.advanceTimersByTimeAsync(2300);
      fake.setStatus({});
      await vi.advanceTimersByTimeAsync(1000);
      expect(screen.queryByRole("heading", { name: "LISTENING…" })).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("level 3 has a name in the level pill", () => {
    render(<Main />);
    owner();
    fake.emit({ type: "auth", ...approvedAuth(3) });
    expect(screen.getByText("L3 · CONFIRM")).toBeInTheDocument();
  });
});
