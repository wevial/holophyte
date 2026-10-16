import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardColumn, ShippedRow, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, peersFetch, settle } from "../harness";
import { blocked, ciPark, DEMO, failed, merged, MINUTE, NOW, SERVE, WRITER } from "./needsFleet";

const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const ready = await Bun.file(new URL("run-merge-ready.json", SERVE)).json();
const template = pinned.columns.flatMap((column) => column.tickets)[0]!;

type State = BoardColumn["state"];
type Item = typeof blocked;

/** The pinned board with its tickets swapped for `keys` by state, backlog
 *  first as a native board sends it. */
function boardOf(keys: Partial<Record<State, string[]>>): Board {
  const states: State[] = ["backlog", ...pinned.columns.map((column) => column.state)];
  return {
    ...pinned,
    columns: states.map((state) => ({
      state,
      tickets: (keys[state] ?? []).map((ticket) => ({ ...template, ticket, title: `${ticket} title` })),
    })),
  };
}

const pullRequestPark = { ...ciPark, ticket: "DEMO-9", run: 9, park_kind: "pull_request" } as Item;

interface Daemon {
  board?: Board;
  items?: Item[];
  shipped?: ShippedRow[];
  mergeReady?: boolean;
}

/** `demo` on DEMO and `writer` on WRITER; a daemon with no `board` answers
 *  `/board` 404. Every url asked is kept. */
function fleet(demo: Daemon, writer: Daemon = {}) {
  const asked: string[] = [];
  const daemon = (project: string, body: Daemon) => ({
    status: { ...working, project, runs: [], queued: [] },
    attention: { level: body.items?.length ? "attention" : "none", now: NOW, project, items: body.items ?? [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: daemon("/srv/dev/demo", demo), [WRITER]: daemon("/srv/dev/writer", writer) });
  const fetch: Fetch = async (url) => {
    asked.push(url);
    for (const [base, body] of [[DEMO, demo], [WRITER, writer]] as const) {
      if (url === `${base}/board`) return body.board ? Response.json(body.board) : new Response("not found", { status: 404 });
      if (url === `${base}/shipped?limit=50`) return Response.json({ rows: body.shipped ?? [], next_before: null, limit: 50 });
      if (url === `${base}/runs/9/merge`) return Response.json({ ...ready, run: 9, ticket: "DEMO-9", ready: body.mergeReady ?? false });
    }
    return polls(url);
  };
  return { fetch, asked };
}

const demoBoard = boardOf({
  backlog: ["DEMO-1"],
  ready: ["DEMO-2"],
  blocked_on_deps: ["DEMO-5"],
  in_flight: ["DEMO-3", "DEMO-11"],
  blocked_on_operator: ["DEMO-8", "DEMO-4"],
});
const demo: Daemon = {
  board: demoBoard,
  items: [blocked, ...failed, ciPark],
  shipped: [merged(10, "DEMO-10", NOW - 30 * MINUTE), merged(7, "DEMO-7", NOW - 13 * 60 * MINUTE)],
};
const writer: Daemon = { board: boardOf({ ready: ["WRITER-9"] }) };

async function mount(hash: string, fetchImpl: Fetch) {
  window.location.hash = hash;
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = NOW;
  const queries = consoleQueryClient();
  render(
    <QueryClientProvider client={queries}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const LABELS = ["Backlog", "Ready", "In flight", "Needs you", "Done today"];
const keysIn = (label: string) =>
  within(screen.getByRole("list", { name: label })).queryAllByRole("listitem").map((card) => card.firstElementChild?.textContent);
const columns = () => Object.fromEntries(LABELS.map((label) => [label, keysIn(label)]));
const heads = () => document.querySelector("[data-column-heads]");
const headCounts = () => Array.from(heads()!.querySelectorAll("[data-count]"), (count) => count.textContent);
const countLine = () => document.querySelector("[data-count-line]")?.textContent;

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => sessionStorage.clear());
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("each board ticket lands in the column its state and Needs you entry name, with today's merges in Done today", async () => {
  await mount("#/tickets?project=demo", fleet(demo, writer).fetch);
  expect(columns()).toEqual({
    Backlog: ["DEMO-1"],
    Ready: ["DEMO-5", "DEMO-2"],
    "In flight": ["DEMO-8", "DEMO-3"],
    "Needs you": ["DEMO-4", "DEMO-11"],
    "Done today": ["DEMO-10"],
  });
  expect(headCounts()).toEqual(["1", "2", "2", "2", "1"]);
  expect(within(screen.getByRole("list", { name: "Backlog" })).getByRole("listitem").textContent).toBe("DEMO-1DEMO-1 title");
  expect(heads()!.className).toContain("sticky");
  expect(document.querySelector("[data-board-scroll]")!.className).toContain("overflow-auto");
});

test("the count line counts every project's tickets with no scope", async () => {
  await mount("#/tickets", fleet(demo, writer).fetch);
  expect(countLine()).toBe("all projects · 9 tickets / 2 in flight / 2 need you / 3 ready");
  expect(screen.queryByText(/^Showing/)).toBeNull();
});

test("a scoped count line counts the project's tickets, and Show all projects drops the scope", async () => {
  sessionStorage.setItem("holophyte.next.project", "demo");
  await mount("#/tickets?project=demo", fleet(demo, writer).fetch);
  expect(countLine()).toBe("demo · 8 tickets / 2 in flight / 2 need you / 2 ready");
  expect(screen.getByText(/^Showing/).textContent).toBe("Showing demo only");
  fireEvent.click(screen.getByRole("button", { name: "Show all projects" }));
  for (let round = 0; round < 2; round += 1) await act(settle);
  expect(window.location.hash).toBe("#/tickets");
  expect(sessionStorage.getItem("holophyte.next.project")).toBeNull();
  expect(countLine()).toStartWith("all projects · 9 tickets");
});

test("a pull request park waits In flight until its merge readiness answers ready, then needs you", async () => {
  const parked = (mergeReady: boolean) =>
    fleet({ board: boardOf({ blocked_on_operator: ["DEMO-9"] }), items: [pullRequestPark], mergeReady }).fetch;
  await mount("#/tickets?project=demo", parked(false));
  expect(keysIn("In flight")).toEqual(["DEMO-9"]);
  expect(keysIn("Needs you")).toEqual([]);
  cleanup();

  await mount("#/tickets?project=demo", parked(true));
  expect(keysIn("In flight")).toEqual([]);
  expect(keysIn("Needs you")).toEqual(["DEMO-9"]);
});

test("the page draws no count line or columns before a board answers, and says when there is no ticket", async () => {
  await mount("#/tickets", fleet({ items: [blocked] }).fetch);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Tickets");
  expect(countLine()).toBeUndefined();
  expect(heads()).toBeNull();
  cleanup();

  const empty = { board: boardOf({}) };
  await mount("#/tickets", fleet(empty, empty).fetch);
  expect(screen.getByText("No open tickets.")).toBeTruthy();
  expect(headCounts()).toEqual(["0", "0", "0", "0", "0"]);
  cleanup();

  await mount("#/tickets?project=demo", fleet(empty, empty).fetch);
  expect(screen.getByText("No open tickets in demo.")).toBeTruthy();
});

test("the Overview and the Tickets page share one /shipped request per host", async () => {
  const { fetch, asked } = fleet(demo, writer);
  await mount("#/", fetch);
  fireEvent.click(screen.getByRole("link", { name: /^Tickets/ }));
  for (let round = 0; round < 4; round += 1) await act(settle);
  expect(keysIn("Done today")).toEqual(["DEMO-10"]);
  for (const base of [DEMO, WRITER]) expect(asked.filter((url) => url === `${base}/shipped?limit=50`)).toHaveLength(1);
});
