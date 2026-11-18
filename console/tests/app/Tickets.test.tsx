import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardColumn, BoardTicket, ShippedRow, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, peersFetch, settle } from "../harness";
import { blocked, ciPark, DEMO, failed, merged, MINUTE, NOW, queuedOf, run, SERVE, WRITER } from "./needsFleet";

const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const ready = await Bun.file(new URL("run-merge-ready.json", SERVE)).json();
const template = pinned.columns.flatMap((column) => column.tickets)[0]!;

type State = BoardColumn["state"];
type Item = typeof blocked;

/** The pinned board with its tickets swapped for `keys` by state, backlog
 *  first as a native board sends it, each with its `facts`. */
function boardOf(keys: Partial<Record<State, string[]>>, facts: Record<string, Partial<BoardTicket>> = {}): Board {
  const states: State[] = ["backlog", ...pinned.columns.map((column) => column.state)];
  return {
    ...pinned,
    columns: states.map((state) => ({
      state,
      tickets: (keys[state] ?? []).map((ticket) => ({ ...template, ticket, title: `${ticket} title`, ...facts[ticket] })),
    })),
  };
}

const pullRequestPark = { ...ciPark, ticket: "DEMO-9", run: 9, park_kind: "pull_request" } as Item;

interface Daemon {
  board?: Board;
  items?: Item[];
  shipped?: ShippedRow[];
  mergeReady?: boolean;
  runs?: Status["runs"];
  queued?: Status["queued"];
  admission?: string;
}

/** `demo` on DEMO and `writer` on WRITER; a daemon with no `board` answers
 *  `/board` 404. Every url asked is kept. */
function fleet(demo: Daemon, writer: Daemon = {}) {
  const asked: string[] = [];
  const daemon = (project: string, body: Daemon) => ({
    status: { ...working, project, runs: body.runs ?? [], queued: body.queued ?? [], admission: body.admission ?? "enabled", now: NOW },
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

async function mount(hash: string, fetchImpl: Fetch, browserNow = NOW) {
  window.location.hash = hash;
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = browserNow;
  const queries = consoleQueryClient();
  render(
    <QueryClientProvider client={queries}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const LABELS = ["Backlog", "Ready", "In flight", "Needs you", "Done today"];
const cardsIn = (label: string) => within(screen.getByRole("list", { name: label })).queryAllByRole("article");
const keysIn = (label: string) => cardsIn(label).map((card) => card.firstElementChild?.firstElementChild?.textContent);
const cardOf = (key: string) => screen.getAllByRole("article").find((card) => card.firstElementChild?.firstElementChild?.textContent === key)!;
const lineOf = (key: string) => cardOf(key).querySelector("[data-state-line]")!;
const glyphOf = (key: string) => lineOf(key).querySelector("[role=img]")!;
const toneOf = (element: Element) => Array.from(element.classList).find((name) => name.startsWith("text-"));
const columns = () => Object.fromEntries(LABELS.map((label) => [label, keysIn(label)]));
const heads = () => document.querySelector("[data-column-heads]");
const headCounts = () => Array.from(heads()!.querySelectorAll("[data-count]"), (count) => count.textContent);
const countLine = () => document.querySelector("[data-page-count]")?.textContent;
const needPart = () => document.querySelector("[data-page-count] .text-need")?.textContent;
const viewGroup = () => screen.queryByRole("group", { name: "View" });

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
});
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
  expect(within(cardOf("DEMO-1")).getByRole("heading").textContent).toBe("DEMO-1 title");
  expect(heads()!.className).toContain("sticky");
  expect(document.querySelector("[data-board-scroll]")!.className).toContain("overflow-auto");
});

test("with no scope the header holds the title Tickets and a count of every project's tickets, the need part in need ink", async () => {
  await mount("#/tickets", fleet(demo, writer).fetch);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Tickets");
  expect(countLine()).toBe("9 tickets · 2 in flight · 2 need you · 3 ready");
  expect(needPart()).toBe("2 need you");
  expect(screen.queryByText(/^Showing/)).toBeNull();
});

test("a scoped count counts the project's tickets without its name or a Showing line, and Show all projects drops the scope", async () => {
  sessionStorage.setItem("holophyte.next.project", "demo");
  await mount("#/tickets?project=demo", fleet(demo, writer).fetch);
  expect(countLine()).toBe("8 tickets · 2 in flight · 2 need you · 2 ready");
  expect(screen.queryByText(/Showing/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Show all projects" }));
  for (let round = 0; round < 2; round += 1) await act(settle);
  expect(window.location.hash).toBe("#/tickets");
  expect(sessionStorage.getItem("holophyte.next.project")).toBeNull();
  expect(countLine()).toStartWith("9 tickets ·");
});

test("the View group offers Board and Table as pressed buttons and a pick holds across a remount, and a state filter draws the table with no View group", async () => {
  await mount("#/tickets", fleet(demo, writer).fetch);
  const pressed = () => within(viewGroup()!).getAllByRole("button").map((button) => [button.textContent, button.getAttribute("aria-pressed")]);
  expect(pressed()).toEqual([["Board", "true"], ["Table", "false"]]);
  fireEvent.click(within(viewGroup()!).getByRole("button", { name: "Table" }));
  await act(settle);
  cleanup();

  await mount("#/tickets", fleet(demo, writer).fetch);
  expect(pressed()).toEqual([["Board", "false"], ["Table", "true"]]);
  expect(document.querySelector("[data-table-scroll]")).not.toBeNull();
  cleanup();

  localStorage.clear();
  await mount("#/tickets?filter=ready", fleet(demo, writer).fetch);
  expect(viewGroup()).toBeNull();
  expect(document.querySelector("[data-table-scroll]")).not.toBeNull();
  expect(document.querySelector("[data-board-scroll]")).toBeNull();
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

test("an in-flight card shows its key, time box, title and the run's phase and review round in the running tone", async () => {
  const board = boardOf({ ready: ["DEMO-2"], in_flight: ["DEMO-3"] }, { "DEMO-3": { time_box_ms: 25 * MINUTE, run: 3 } });
  const reviewing = { ...run(3, "DEMO-3"), phase: "reviewing", round: 1, max_rounds: 3 };
  await mount("#/tickets?project=demo", fleet({ board, runs: [reviewing] }).fetch);
  const card = cardOf("DEMO-3");
  expect(card.querySelector("[data-time-box]")!.textContent).toBe("25m time box");
  expect(card.querySelector("[data-time-box] .sr-only")?.textContent).toBe(" time box");
  expect(within(card).getByRole("heading").textContent).toBe("DEMO-3 title");
  const foot = within(card).getByRole("heading").nextElementSibling!;
  expect(Array.from(foot.children, (part) => part.hasAttribute("data-state-line") || part.hasAttribute("data-time-box"))).toEqual([true, true]);
  expect(foot.classList).toContain("flex");
  expect(foot.classList).not.toContain("flex-col");
  expect(lineOf("DEMO-3").textContent).toBe("reviewing · round 2 of 3");
  expect(glyphOf("DEMO-3").getAttribute("aria-label")).toBe("running");
  expect(toneOf(glyphOf("DEMO-3"))).toBe("text-live");
  expect(cardOf("DEMO-2").querySelector("[data-time-box]")).toBeNull();
});

test("each card's line says what its ticket waits on, why it failed, how long it has asked by the daemon's clock or since its run last beat", async () => {
  const board = boardOf(
    { needs_spec: ["DEMO-1"], ready: ["DEMO-2"], blocked_on_deps: ["DEMO-5"], in_flight: ["DEMO-6", "DEMO-11"], blocked_on_operator: ["DEMO-8", "DEMO-4"] },
    { "DEMO-5": { waits_on: ["DEMO-2"] } },
  );
  const items = [
    blocked,
    ...failed.map((item) => ({ ...item, reason: "verify failed\n2 tests red" })),
    { ...ciPark, pr_url: "https://github.com/example/repo/pull/523" } as Item,
  ];
  const quiet = { ...run(6, "DEMO-6"), heartbeat_age_ms: 15 * MINUTE };
  const queued = [{ ...queuedOf("DEMO-2")[0]!, reason: "next free worker" }];
  await mount("#/tickets?project=demo", fleet({ board, items, runs: [quiet], queued }).fetch, NOW + 60 * MINUTE);
  const lines = ["DEMO-8", "DEMO-5", "DEMO-2", "DEMO-1", "DEMO-11", "DEMO-4", "DEMO-6"].map((key) => lineOf(key).textContent);
  expect(lines).toEqual([
    "on CI · PR #523", "blocked by DEMO-2", "next free worker", "needs a spec",
    "failed · verify failed", "question open 8h 02m", "stale · no beat 15m",
  ]);
  const glyphs = ["DEMO-8", "DEMO-11", "DEMO-4", "DEMO-6"].map((key) => [glyphOf(key).getAttribute("data-state"), toneOf(glyphOf(key))]);
  expect(glyphs).toEqual([["on_ci", "text-wait"], ["failed", "text-bad"], ["question", "text-ask-ink"], ["stale", "text-need"]]);
  expect(glyphOf("DEMO-8").querySelector(".state-march")).not.toBeNull();
});

test("a held host's ready card says it is held, and a merge of today says when it merged and keeps its time box", async () => {
  const board = boardOf({ ready: ["DEMO-2"] });
  await mount("#/tickets?project=demo", fleet({ board, admission: "held", shipped: [{ ...merged(10, "DEMO-10", NOW - 45 * MINUTE), estimate_min: 25 }] }).fetch);
  expect(lineOf("DEMO-2").textContent).toBe("held with demo");
  expect(glyphOf("DEMO-2").getAttribute("data-state")).toBe("held");
  expect(lineOf("DEMO-10").textContent).toBe("merged 11:15");
  expect(cardOf("DEMO-10").querySelector("[data-time-box]")!.textContent).toBe("25m time box");
});

test("cards keep their need, done, backlog and plain looks, and no card title uses the reading face", async () => {
  await mount("#/tickets?project=demo", fleet(demo).fetch);
  const looks = ["Needs you", "Done today", "Backlog", "Ready"].map((label) => cardsIn(label)[0]!);
  expect(looks.map((card) => card.getAttribute("data-look"))).toEqual(["need", "done", "backlog", "plain"]);
  for (const card of screen.getAllByRole("article")) expect(within(card).getByRole("heading").classList).not.toContain("font-read");
  const [need, done, backlog, plain] = looks;
  expect(need!.className).toContain("var(--need-edge)");
  expect(backlog!.className).toContain("border-dashed");
  expect(backlog!.classList).toContain("bg-transparent");
  expect(backlog!.classList).not.toContain("bg-raised");
  for (const card of [need!, done!, plain!]) expect(card.className).not.toContain("border-dashed");
  expect(plain!.className).not.toContain("var(--need-edge)");
  const inkOf = (card: Element) => Array.from(within(card as HTMLElement).getByRole("heading").classList).find((name) => name.startsWith("text-ink"));
  expect([done!, backlog!, plain!].map(inkOf)).toEqual(["text-ink-2", "text-ink-3", "text-ink"]);
});

const viewsFleet = () =>
  fleet({
    board: boardOf({ backlog: ["DEMO-1"], needs_spec: ["DEMO-6", "DEMO-7"], ready: ["DEMO-2"], blocked_on_deps: ["DEMO-5"] }),
    items: [{ ...blocked, ticket: "DEMO-2", run: 2 }],
  }).fetch;
const viewLinks = () => within(screen.getByRole("list", { name: "Ticket views" })).getAllByRole("link");
const shownKeys = () => screen.queryAllByRole("article").map((card) => card.firstElementChild?.firstElementChild?.textContent).sort();

test("the sidebar's Views list Ready, Needs spec and Backlog with the project's ticket count in each, a Needs you ticket in none", async () => {
  await mount("#/?project=demo", viewsFleet());
  const links = viewLinks();
  expect(links.map((link) => link.getAttribute("aria-label"))).toEqual(["Ready", "Needs spec", "Backlog"]);
  const counts = links.map((link) => link.querySelector("[data-count]")!);
  expect(counts.map((count) => count.textContent)).toEqual(["1", "2", "1"]);
  for (const count of counts) expect(count.classList).toContain("font-mono");
});

test("a view link shows only its tickets and marks itself current with a zero need you still in need ink, and All drops the filter but keeps the scope", async () => {
  await mount("#/?project=demo", viewsFleet());
  fireEvent.click(screen.getByRole("link", { name: "Needs spec" }));
  for (let round = 0; round < 2; round += 1) await act(settle);
  expect(window.location.hash).toBe("#/tickets?project=demo&filter=needs_spec");
  const rowKeys = Array.from(document.querySelectorAll("[data-row] [data-cell=key]"), (cell) => cell.textContent).sort();
  expect(rowKeys).toEqual(["DEMO-6", "DEMO-7"]);
  expect(countLine()).toBe("2 tickets · 0 in flight · 0 need you · 0 ready");
  expect(needPart()).toBe("0 need you");
  expect(viewLinks().map((link) => link.getAttribute("aria-current"))).toEqual([null, "page", null]);
  const state = screen.getByRole("button", { name: /^State: / });
  expect(state.textContent).toBe("Needs spec");

  fireEvent.click(state);
  for (let round = 0; round < 2; round += 1) await act(settle);
  fireEvent.click(within(screen.getByRole("listbox", { name: "State" })).getByRole("option", { name: "All states" }));
  for (let round = 0; round < 2; round += 1) await act(settle);
  expect(window.location.hash).toBe("#/tickets?project=demo");
  expect(shownKeys()).toEqual(["DEMO-1", "DEMO-2", "DEMO-5", "DEMO-6", "DEMO-7"]);
});

test("an unknown filter lists every ticket as no filter does", async () => {
  await mount("#/tickets?filter=nonsense", fleet(demo, writer).fetch);
  expect(countLine()).toBe("9 tickets · 2 in flight · 2 need you · 3 ready");
});
