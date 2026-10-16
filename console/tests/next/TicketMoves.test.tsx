import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardTicket, Status, TicketDetail } from "../../src/lib/contract.gen";
import { addressOf } from "../../src/lib/hosts";
import type { Fetch } from "../../src/lib/poll";
import { storeToken } from "../../src/lib/token";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, MINUTE, run, SERVE } from "./needsFleet";

const TOKEN = "tok-356";

const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const template = pinned.columns.flatMap((column) => column.tickets)[0]!;
const servedTicket = (await Bun.file(new URL("ticket.json", SERVE)).json()) as TicketDetail;

const detail = (key: string, revision: number, column: string | null, facts: Partial<TicketDetail>): TicketDetail => ({
  ...servedTicket,
  ticket: key,
  title: `${key} title`,
  runs: [],
  steers: [],
  current: { ...servedTicket.current!, revision, column },
  ...facts,
});
const tickets: Record<string, TicketDetail> = {
  "DEMO-1": detail("DEMO-1", 2, "backlog", { run: null, status: "needs_spec" }),
  "DEMO-2": detail("DEMO-2", 3, "ready", { run: null, status: "ready" }),
  "DEMO-3": detail("DEMO-3", 5, "ready", { run: 9, status: "in_flight" }),
};
const card = (key: string, facts: Partial<BoardTicket>) => ({ ...template, ticket: key, title: `${key} title`, ...facts }) as BoardTicket;
const board = (editable: boolean): Board => ({
  ...pinned,
  editable,
  stories: [],
  columns: [
    { state: "backlog", tickets: [card("DEMO-1", { column: "backlog", revision: 2, run: null })] },
    { state: "ready", tickets: [card("DEMO-2", { column: "ready", revision: 3, run: null })] },
    { state: "in_flight", tickets: [card("DEMO-3", { column: "ready", revision: 5, run: 9, time_box_ms: 25 * MINUTE })] },
  ],
});

let requested: string[] = [];
let ifMatch: (string | null)[] = [];

/** DEMO runs DEMO-3 as run 9 behind a `/board` that is `editable` as given;
 *  every `POST /tickets/KEY/move` or `/cancel` is answered by `write`. */
async function mount(key: string, { editable = true, write }: { editable?: boolean; write?: Fetch } = {}) {
  window.location.hash = `#/tickets/demo/${key}`;
  const polls = fleet({ items: [], runs: [{ ...run(9, "DEMO-3"), phase: "working" }] }, { items: [] });
  requested = [];
  ifMatch = [];
  const fake = fakeDeps(async (url, init) => {
    requested.push(url);
    if (init?.method === "POST" && /\/tickets\/[^/]+\/(move|cancel)$/.test(url) && write != null) {
      ifMatch.push(new Headers(init.headers).get("if-match"));
      return write(url, init);
    }
    if (url === `${DEMO}/status`) return Response.json({ ...((await (await polls(url)).json()) as Status), actions: true });
    if (url === `${DEMO}/board`) return Response.json(board(editable));
    const ticket = url.startsWith(`${DEMO}/tickets/`) ? decodeURIComponent(url.slice(`${DEMO}/tickets/`.length)) : null;
    if (ticket != null) return ticket in tickets ? Response.json(tickets[ticket]) : new Response("not found", { status: 404 });
    if (url.endsWith("/board")) return new Response("not found", { status: 404 });
    return polls(url);
  });
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
}

const actions = () => document.querySelector<HTMLElement>("[data-ticket-actions]")!;
const toast = () => document.querySelector("[data-toast]")?.textContent ?? "";
const reads = (path: string) => requested.filter((url) => url === `${DEMO}${path}`).length;
const dialog = () => screen.getByRole("dialog", { name: "Cancel DEMO-3?" });

async function press(name: string, scope: HTMLElement = actions()) {
  await act(async () => void fireEvent.click(within(scope).getByRole("button", { name })));
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
beforeEach(() => storeToken(addressOf(DEMO), TOKEN));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("Move to backlog posts the move once at the read revision with the bearer, toasts and re-reads the board", async () => {
  const write = fakeFetch({ ticket: "DEMO-2", revision: 4 });
  await mount("DEMO-2", { write: write.fetchImpl });
  expect(within(actions()).getAllByRole("button").map((button) => button.textContent)).toEqual(["Steer", "Move to backlog", "Cancel ticket"]);
  const boardReads = reads("/board");
  await press("Move to backlog");
  expect(write.seen.map(({ url, body, authorization }) => ({ url, body, authorization }))).toEqual([
    { url: `${DEMO}/tickets/DEMO-2/move`, body: { column: "backlog" }, authorization: `Bearer ${TOKEN}` },
  ]);
  expect(ifMatch).toEqual(["3"]);
  expect(toast()).toBe("Moved DEMO-2 to Backlog.");
  expect(reads("/board")).toBeGreaterThan(boardReads);
});

test("a refused Requeue shows the daemon's problems under the buttons and toasts nothing; an in-flight ticket draws no Move to backlog", async () => {
  const write = fakeFetch(() => Response.json({ problems: ["missing section '## Summary'"] }, { status: 422 }));
  await mount("DEMO-1", { write: write.fetchImpl });
  expect(within(actions()).queryByRole("button", { name: "Move to backlog" })).toBeNull();
  await press("Requeue");
  expect(write.seen.map(({ url, body }) => ({ url, body }))).toEqual([{ url: `${DEMO}/tickets/DEMO-1/move`, body: { column: "ready" } }]);
  expect(within(actions()).getByRole("alert").textContent).toBe("missing section '## Summary'");
  expect(toast()).toBe("");
  cleanup();
  await mount("DEMO-3");
  expect(within(actions()).getByRole("button", { name: "Cancel ticket" })).toBeTruthy();
  expect(within(actions()).queryByRole("button", { name: "Move to backlog" })).toBeNull();
});

test("a move answered 409 says the ticket changed and reads it again", async () => {
  const write = fakeFetch(() => Response.json({ error: "revision mismatch", current: 4 }, { status: 409 }));
  await mount("DEMO-2", { write: write.fetchImpl });
  const ticketReads = reads("/tickets/DEMO-2");
  await press("Move to backlog");
  expect(within(actions()).getByRole("alert").textContent).toBe("DEMO-2 changed since this page read it. It is read again; try once more.");
  expect(reads("/tickets/DEMO-2")).toBeGreaterThan(ticketReads);
  expect(write.seen).toHaveLength(1);
});

test("Cancel ticket asks for a reason before posting it as the note, then closes and toasts", async () => {
  const write = fakeFetch({ ticket: "DEMO-3", revision: 6 });
  await mount("DEMO-3", { write: write.fetchImpl });
  await press("Cancel ticket");
  expect(dialog().textContent).toContain("Run 9 stops now.");
  const confirm = within(dialog()).getByRole("button", { name: "Cancel ticket" });
  expect(confirm.hasAttribute("disabled")).toBe(true);
  await act(async () => void fireEvent.change(within(dialog()).getByRole("textbox", { name: "Reason · saved with the ticket" }), { target: { value: "wrong scope" } }));
  expect(confirm.hasAttribute("disabled")).toBe(false);
  await press("Cancel ticket", dialog());
  expect(write.seen.map(({ url, body }) => ({ url, body }))).toEqual([{ url: `${DEMO}/tickets/DEMO-3/cancel`, body: { note: "wrong scope" } }]);
  expect(ifMatch).toEqual(["5"]);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(toast()).toBe("Cancelled DEMO-3.");
});

test("Esc or Keep it closes the cancel dialog and posts nothing", async () => {
  const write = fakeFetch({ ticket: "DEMO-3", revision: 6 });
  await mount("DEMO-3", { write: write.fetchImpl });
  await press("Cancel ticket");
  await act(async () => void fireEvent.keyDown(document, { key: "Escape" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  await press("Cancel ticket");
  await press("Keep it", dialog());
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(write.seen).toEqual([]);
});

test("a host whose /board is not editable draws none of the moves", async () => {
  await mount("DEMO-2", { editable: false });
  expect(within(actions()).getAllByRole("button").map((button) => button.textContent)).toEqual(["Steer"]);
});
