import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardColumn, BoardTicket } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, MINUTE, NOW, run, SERVE } from "./needsFleet";

const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const template = pinned.columns.flatMap((column) => column.tickets)[0]!;

const ticket = (key: string, facts: Partial<BoardTicket>): BoardTicket => ({ ...template, ticket: key, title: `${key} title`, ...facts });
const column = (state: BoardColumn["state"], tickets: BoardTicket[]): BoardColumn => ({ state, tickets });

const board: Board = {
  ...pinned,
  columns: [
    column("ready", [ticket("DEMO-2", { time_box_ms: null, run: null })]),
    column("in_flight", [ticket("DEMO-3", { time_box_ms: 25 * MINUTE, run: 3 })]),
  ],
};

/** DEMO serves the board above with DEMO-3's run 3 working; WRITER has no board. */
function boardFetch(): Fetch {
  const polls = fleet({ items: [], runs: [{ ...run(3, "DEMO-3"), phase: "working", round: 0 }] }, { items: [] });
  return async (url) => {
    if (url === `${DEMO}/board`) return Response.json(board);
    if (url.endsWith("/board")) return new Response("not found", { status: 404 });
    return polls(url);
  };
}

async function mount(hash: string) {
  window.location.hash = hash;
  const fake = fakeDeps(boardFetch());
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const cardLink = (key: string) => document.getElementById(`card-demo-${key}`)!;
const peek = () => screen.queryByRole("dialog");
const fact = (label: string) => {
  const term = within(peek()!).getByText(label, { selector: "dt" });
  return term.nextElementSibling as HTMLElement;
};

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("a card's click opens its peek with key, Expand, column chip and facts, and another card's click swaps it in place", async () => {
  await mount("#/tickets");
  expect(cardLink("DEMO-3").getAttribute("aria-haspopup")).toBe("dialog");
  await act(async () => void fireEvent.click(cardLink("DEMO-3")));

  const dialog = screen.getByRole("dialog", { name: "DEMO-3 title" });
  expect(dialog.querySelector("[data-peek-key]")!.textContent).toBe("DEMO-3");
  expect(within(dialog).getByRole("link", { name: /^Expand/ }).getAttribute("href")).toEndWith("#/tickets/demo/DEMO-3");
  expect(document.activeElement).toBe(within(dialog).getByRole("button", { name: "Close preview" }));
  expect(dialog.querySelector("[data-peek-column]")!.textContent).toBe("In flight");
  expect(fact("Project").textContent).toBe("demo");
  expect(fact("Time box").textContent).toBe("25m");
  expect(within(fact("Run")).getByText("run 3")).toBeTruthy();
  expect(fact("Run").querySelector("[data-peek-run-state]")!.textContent).toBe("implementing");
  expect(cardLink("DEMO-3").hasAttribute("data-open")).toBe(true);

  await act(async () => void fireEvent.click(cardLink("DEMO-2")));
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  expect(screen.getByRole("dialog", { name: "DEMO-2 title" })).toBeTruthy();
  expect(fact("Run").textContent).toBe("none yet");
  expect(fact("Time box").textContent).toBe("none");
  expect(cardLink("DEMO-2").hasAttribute("data-open")).toBe(true);
  expect(cardLink("DEMO-3").hasAttribute("data-open")).toBe(false);
});

test("Esc or Close preview shuts the peek and puts focus back on the card that opened it", async () => {
  await mount("#/tickets");
  await act(async () => void fireEvent.click(cardLink("DEMO-2")));
  expect(peek()).not.toBeNull();
  await act(async () => void fireEvent.keyDown(document.activeElement!, { key: "Escape" }));
  expect(peek()).toBeNull();
  expect(document.activeElement).toBe(cardLink("DEMO-2"));

  await act(async () => void fireEvent.click(cardLink("DEMO-2")));
  await act(async () => void fireEvent.click(screen.getByRole("button", { name: "Close preview" })));
  expect(peek()).toBeNull();
  expect(document.activeElement).toBe(cardLink("DEMO-2"));
});

test("Esc another handler already took leaves the peek open", async () => {
  await mount("#/tickets");
  await act(async () => void fireEvent.click(cardLink("DEMO-2")));
  const taken = (event: Event) => event.preventDefault();
  document.activeElement!.addEventListener("keydown", taken);
  await act(async () => void fireEvent.keyDown(document.activeElement!, { key: "Escape" }));
  expect(peek()).not.toBeNull();
});

test("a Ctrl click on a card opens no peek and leaves the click's default to the browser", async () => {
  await mount("#/tickets");
  let notPrevented = false;
  await act(async () => {
    notPrevented = fireEvent.click(cardLink("DEMO-3"), { ctrlKey: true });
  });
  expect(notPrevented).toBe(true);
  expect(peek()).toBeNull();
});

test("the ticket route shows its key and a link to the classic Board, with Tickets current in the sidebar", async () => {
  await mount("#/tickets/demo/DEMO-3");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("DEMO-3");
  expect(screen.getByText("The ticket page comes here.")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Open the Board in the classic console" }).getAttribute("href")).toEndWith("?classic#view=board");
  const pages = within(screen.getByRole("navigation", { name: "Pages" })).getAllByRole("link");
  expect(pages.filter((link) => link.getAttribute("aria-current") === "page").map((link) => link.getAttribute("aria-label"))).toEqual(["Tickets"]);
});
