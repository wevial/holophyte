import { afterAll, afterEach, beforeAll, beforeEach, expect, setSystemTime, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardColumn, BoardTicket, ShippedRow, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, peersFetch, settle } from "../harness";
import { DEMO, MINUTE, NOW, SERVE, WRITER } from "./needsFleet";

const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const template = pinned.columns.flatMap((column) => column.tickets)[0]!;

type State = BoardColumn["state"];

/** The pinned board with its tickets swapped for `keys` by state, each
 *  titled "KEY title" with no label unless `facts` says otherwise. */
function boardOf(keys: Partial<Record<State, string[]>>, facts: Record<string, Partial<BoardTicket>> = {}): Board {
  const states: State[] = ["backlog", ...pinned.columns.map((column) => column.state)];
  return {
    ...pinned,
    columns: states.map((state) => ({
      state,
      tickets: (keys[state] ?? []).map((ticket) => ({ ...template, ticket, title: `${ticket} title`, labels: [], ...facts[ticket] })),
    })),
  };
}

/** `demo` on DEMO with `board`, and `writer` on WRITER with no board; each
 *  answers `/tickets?q=TEXT` from `searches` by its text, 503 for a null
 *  entry, else 404. Every
 *  url asked is kept. */
function fleet(board: Board, searches: Record<string, string[] | null> = {}) {
  const asked: string[] = [];
  const daemon = (project: string) => ({
    status: { ...working, project, runs: [], queued: [], admission: "enabled", now: NOW },
    attention: { level: "none", now: NOW, project, items: [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: daemon("/srv/dev/demo"), [WRITER]: daemon("/srv/dev/writer") });
  const fetch: Fetch = async (url) => {
    asked.push(url);
    if (url === `${DEMO}/board`) return Response.json(board);
    for (const base of [DEMO, WRITER]) {
      if (url === `${base}/shipped?limit=50`) return Response.json({ rows: [] as ShippedRow[], next_before: null, limit: 50 });
    }
    const search = url.startsWith(`${DEMO}/tickets?q=`) ? searches[decodeURIComponent(url.slice(`${DEMO}/tickets?q=`.length))] : undefined;
    if (search === null) return new Response("store locked", { status: 503 });
    if (search) return Response.json({ q: "", tickets: search });
    return polls(url);
  };
  return { fetch, asked };
}

const demoBoard = boardOf(
  { backlog: ["DEMO-1"], ready: ["DEMO-2"], blocked_on_deps: ["DEMO-5"], in_flight: ["DEMO-3"] },
  { "DEMO-3": { title: "Rename the lane heads" }, "DEMO-2": { labels: ["ux"] } },
);

async function mount(hash: string, fetchImpl: Fetch) {
  window.location.hash = hash;
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const field = () => screen.getByRole("searchbox", { name: "Search tickets" }) as HTMLInputElement;

/** Type `text` into Search and wait out its debounce. */
async function search(text: string) {
  fireEvent.change(field(), { target: { value: text } });
  await act(() => new Promise((resolve) => setTimeout(resolve, 300)));
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const cardKeys = () => screen.queryAllByRole("article").map((card) => card.firstElementChild?.firstElementChild?.textContent);
const rowKeys = () => Array.from(document.querySelectorAll("[data-row] [data-cell=key]"), (cell) => cell.textContent);
const heads = () => document.querySelector("[data-column-heads]");
const headCounts = () => Array.from(heads()!.querySelectorAll("[data-count]"), (count) => count.textContent);
const stateGroup = () => screen.getByRole("group", { name: "State" });
const query = () => new URLSearchParams(window.location.hash.split("?")[1] ?? "");

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

test("a search keeps the cards whose key, title or label holds it, ignoring case, when the search route is missing", async () => {
  await mount("#/tickets", fleet(demoBoard).fetch);
  await search("demo-3");
  expect(cardKeys()).toEqual(["DEMO-3"]);
  await search("LANE");
  expect(cardKeys()).toEqual(["DEMO-3"]);
  await search("UX");
  expect(cardKeys()).toEqual(["DEMO-2"]);
});

test("a search keeps a card its host's search route names by body", async () => {
  const { fetch, asked } = fleet(demoBoard, { quokka: ["DEMO-5"] });
  await mount("#/tickets", fetch);
  await search("quokka");
  expect(cardKeys()).toEqual(["DEMO-5"]);
  expect(asked).toContain(`${DEMO}/tickets?q=quokka`);
});

test("a host whose search read fails after an answer no longer names a card by body", async () => {
  const searches: Record<string, string[] | null> = { quokka: ["DEMO-5"] };
  await mount("#/tickets", fleet(demoBoard, searches).fetch);
  await search("quokka");
  expect(cardKeys()).toEqual(["DEMO-5"]);
  await search("demo-3");
  searches.quokka = null;
  setSystemTime(new Date(Date.now() + MINUTE));
  try {
    await search("quokka");
  } finally {
    setSystemTime();
  }
  expect(cardKeys()).toEqual([]);
});

test("the debounced search is the address's q, and a fresh mount on it searches again", async () => {
  await mount("#/tickets", fleet(demoBoard).fetch);
  await search("demo-3");
  expect(query().get("q")).toBe("demo-3");
  const hash = window.location.hash;
  cleanup();

  await mount(hash, fleet(demoBoard).fetch);
  expect(field().value).toBe("demo-3");
  expect(cardKeys()).toEqual(["DEMO-3"]);
});

test("slash focuses Search from the page, and Escape in it empties it and drops q", async () => {
  await mount("#/tickets", fleet(demoBoard).fetch);
  document.body.focus();
  const typed = fireEvent.keyDown(document.body, { key: "/" });
  expect(typed).toBe(false);
  expect(document.activeElement).toBe(field());
  expect(field().value).toBe("");

  await search("demo");
  expect(query().get("q")).toBe("demo");
  fireEvent.keyDown(field(), { key: "Escape" });
  for (let round = 0; round < 2; round += 1) await act(settle);
  expect(field().value).toBe("");
  expect(query().has("q")).toBe(false);
});

test("a picked state lists its tickets as Table rows with no lanes, heads or view switch", async () => {
  await mount("#/tickets?project=demo", fleet(demoBoard).fetch);
  const buttons = within(stateGroup()).getAllByRole("button");
  expect(buttons.map((button) => button.textContent)).toEqual(["All", "Ready", "In flight", "Needs you", "Backlog", "Needs spec", "Done"]);
  expect(buttons.map((button) => button.getAttribute("aria-pressed"))).toEqual(["true", "false", "false", "false", "false", "false", "false"]);

  fireEvent.click(within(stateGroup()).getByRole("button", { name: "Ready" }));
  for (let round = 0; round < 2; round += 1) await act(settle);
  expect(window.location.hash).toBe("#/tickets?project=demo&filter=ready");
  expect(within(stateGroup()).getByRole("button", { name: "Ready" }).getAttribute("aria-pressed")).toBe("true");
  expect(rowKeys().sort()).toEqual(["DEMO-2", "DEMO-5"]);
  expect(heads()).toBeNull();
  expect(screen.queryAllByRole("article")).toEqual([]);
  expect(screen.queryByRole("group", { name: "View" })).toBeNull();
});

test("a search under All counts only its matches in the column heads and the count line", async () => {
  const board = boardOf(
    { backlog: ["DEMO-1"], ready: ["DEMO-2"], in_flight: ["DEMO-3", "DEMO-6"] },
    { "DEMO-1": { title: "Billing export" }, "DEMO-2": { title: "Login copy" }, "DEMO-3": { title: "Login form" }, "DEMO-6": { title: "Login lane" } },
  );
  await mount("#/tickets?project=demo", fleet(board).fetch);
  await search("login");
  expect(headCounts()).toEqual(["0", "1", "2", "0", "0"]);
  expect(document.querySelector("[data-count-line]")?.textContent).toBe("demo · 3 tickets / 2 in flight / 0 need you / 1 ready");
});

test("a search or a state that leaves no ticket reads No tickets match in place of the board", async () => {
  await mount("#/tickets", fleet(demoBoard).fetch);
  await search("nothing holds this");
  expect(screen.getAllByText("No tickets match")).toHaveLength(1);
  expect(heads()).toBeNull();
  expect(screen.queryAllByRole("article")).toEqual([]);
  expect(rowKeys()).toEqual([]);
  cleanup();

  await mount("#/tickets", fleet(boardOf({ backlog: ["DEMO-1"], ready: ["DEMO-2"] })).fetch);
  fireEvent.click(within(stateGroup()).getByRole("button", { name: "In flight" }));
  for (let round = 0; round < 2; round += 1) await act(settle);
  expect(screen.getAllByText("No tickets match")).toHaveLength(1);
  expect(rowKeys()).toEqual([]);
});
