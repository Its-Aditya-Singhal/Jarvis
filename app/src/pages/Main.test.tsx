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
