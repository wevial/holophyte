import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createHashHistory, createRootRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import type { HistoryRow, HistoryStateCounts, Status } from "../../src/lib/contract.gen";
import type { HostRecord } from "../../src/lib/hosts";
import type { Fetch } from "../../src/lib/poll";
import type { PeersState } from "../../src/hooks/usePeers";
import { consoleQueryClient, type ContractBodies } from "../../src/app/fleet";
import { useRunPage, type RunsSearch } from "../../src/app/history";
import { ConsoleApp } from "../../src/app/router";
import { PeersContext, PollDepsContext } from "../../src/app/scope";
import { fakeDeps, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const WRITER = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;

const MINUTE = 60_000;
const today = (hour: number, minute: number) => new Date(2026, 9, 15, hour, minute).getTime();
const NOW = today(18, 0);
const SINCE = new Date(2026, 9, 9, 0, 0).getTime();

const run = (row: Partial<HistoryRow> & Pick<HistoryRow, "id" | "ticket" | "title" | "started_ms">): HistoryRow => ({
  ticket_url: null, attempt: 1, phase: "reviewing", outcome: null, outcome_reason: null, failure_kind: null,
  park_kind: null, ended_ms: null, elapsed_ms: 16 * MINUTE, heartbeat_age_ms: 1_000, rounds: 0, max_rounds: 3,
  working_ms: null, estimate_ms: null, pr_url: null, host: null, ...row,
});

/** `count` open runs, one a minute back from 17:59, with ids from `first` down. */
const runs = (prefix: string, first: number, count: number) =>
  Array.from({ length: count }, (_, at) => run({ id: first - at, ticket: `${prefix}-${first - at}`, title: `Run ${first - at}`, started_ms: today(17, 59) - at * MINUTE }));

interface Daemon {
  rows: HistoryRow[] | ((params: URLSearchParams) => HistoryRow[]);
  total?: number;
  counts?: HistoryStateCounts;
}

/** Each daemon answers `/history` at local midnight six days before today's with its rows in the order given, cut at
 *  `offset` and `limit`, the limit capped at 5000 as the daemon caps it, and with `total` and `counts` as given (else every row and none);
 *  every `/history` query it is asked lands in `asked`. */
function fleet(demo: Daemon, writer: Daemon) {
  const asked: { base: string; params: URLSearchParams }[] = [];
  const bodies = (project: string) => ({
    status: { ...working, project, runs: [], queued: [] },
    attention: { level: "none", now: NOW, project, items: [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: bodies("/srv/dev/demo"), [WRITER]: bodies("/srv/dev/writer") });
  const fetchImpl: Fetch = async (url) => {
    for (const [base, daemon] of [[DEMO, demo], [WRITER, writer]] as const) {
      if (!url.startsWith(`${base}/history?`)) continue;
      const params = new URL(url).searchParams;
      asked.push({ base, params });
      if (params.get("since") !== String(SINCE)) return new Response("bad since", { status: 400 });
      const offset = Number(params.get("offset") ?? 0);
      const limit = Math.min(Number(params.get("limit") ?? 200), 5000);
      const ordered = typeof daemon.rows === "function" ? daemon.rows(params) : daemon.rows;
      const total = daemon.total ?? ordered.length;
      return Response.json({
        rows: ordered.slice(offset, offset + limit), since: SINCE, until: null, limit, offset, truncated: total > offset + limit, total,
        counts: { state: daemon.counts ?? { active: 0, merged: 0, failed: 0, abandoned: 0 }, failure: {}, implementer: {} }, now: NOW,
      });
    }
    return polls(url);
  };
  const askedOf = (base: string) => asked.filter((each) => each.base === base).map((each) => Object.fromEntries(each.params));
  return { fetchImpl, askedOf };
}

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
  return fake.clock;
}

const list = () => document.querySelector("ol[aria-label^='Runs, ']")!;
const rows = () => within(list() as HTMLElement).queryAllByRole("listitem");
const shownIds = () => rows().map((row) => row.getAttribute("data-run"));
const pager = () => screen.queryByRole("navigation", { name: "Pages of runs" });
const range = () => pager()!.querySelector("span")!.textContent;
const hashSearch = () => new URLSearchParams(window.location.hash.split("?")[1]);
const press = async (element: HTMLElement) => {
  await act(async () => {
    fireEvent.click(element);
    await settle();
  });
  for (let round = 0; round < 3; round += 1) await act(settle);
};
const button = (name: string) => screen.getByRole("button", { name });
const paged = (state: string | null, offset: number, limit: number, sort = "started", order = "desc") => ({
  since: String(SINCE), ...(state ? { state } : {}), sort, order, offset: String(offset), limit: String(limit),
});

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
});
afterAll(() => {
  happyDOM.setURL("about:blank");
  if (zone == null) delete process.env.TZ;
  else process.env.TZ = zone;
});

test("Runs asks each host once for the first 50 runs of the last 7 days newest first, in no state, and reads the summed total in its pager", async () => {
  const daemons = fleet({ rows: runs("DEMO", 100, 3), total: 3 }, { rows: runs("WRITER", 200, 2), total: 2 });
  await mount("#/runs", daemons.fetchImpl);
  expect(daemons.askedOf(DEMO)).toEqual([paged(null, 0, 50)]);
  expect(daemons.askedOf(WRITER)).toEqual([paged(null, 0, 50)]);
  expect(button("Sort by started").getAttribute("aria-pressed")).toBe("true");
  expect(list().getAttribute("aria-label")).toBe("Runs, newest first");
  expect(rows()).toHaveLength(5);
  expect(range()).toBe("1–5 of 5");
});

test("one host in scope serves each page itself, Next walks to the last page, and a new page size goes back to page 1", async () => {
  const daemons = fleet({ rows: runs("DEMO", 120, 120) }, { rows: runs("WRITER", 200, 5) });
  await mount("#/runs?project=demo&state=all", daemons.fetchImpl);
  expect(daemons.askedOf(WRITER)).toEqual([]);
  expect(daemons.askedOf(DEMO)).toEqual([paged(null, 0, 50)]);
  expect(range()).toBe("1–50 of 120");

  await press(button("Next"));
  expect(hashSearch().get("page")).toBe("2");
  expect(daemons.askedOf(DEMO).at(-1)).toEqual(paged(null, 50, 50));
  expect(range()).toBe("51–100 of 120");
  expect(shownIds()[0]).toBe("70");

  await press(button("Next"));
  expect(range()).toBe("101–120 of 120");
  expect(button("Next").hasAttribute("disabled")).toBe(true);

  const size = screen.getByRole("combobox", { name: "Rows a page" });
  const controls = document.querySelector("[data-controls]")!;
  expect(Array.from(controls.querySelectorAll("button, select")).at(-1)).toBe(size);
  expect(pager()!.contains(size)).toBe(false);
  await act(async () => {
    fireEvent.change(size, { target: { value: "25" } });
    await settle();
  });
  for (let round = 0; round < 3; round += 1) await act(settle);
  expect(hashSearch().get("size")).toBe("25");
  expect(hashSearch().has("page")).toBe(false);
  expect(daemons.askedOf(DEMO).at(-1)).toEqual(paged(null, 0, 25));
  expect(range()).toBe("1–25 of 120");
});

function PageOfRuns({ search }: { search: Required<RunsSearch> }) {
  const { rows } = useRunPage(search);
  return <ol aria-label="Runs, newest first">{rows.map((row) => <li key={`${row.base} ${row.id}`}>{row.ticket}</li>)}</ol>;
}

async function mountPage(search: Required<RunsSearch>, fetchImpl: Fetch) {
  const fake = fakeDeps(fetchImpl);
  const host = (base: string, project: string): HostRecord<ContractBodies> => ({
    key: base.replace("http://", ""), address: base.replace("http://", ""), base, label: base, project: null,
    status: { ...working, project, runs: [], queued: [] }, attention: { level: "none", now: NOW, project, items: [] },
    polled_ms: NOW, seen_ms: NOW, error: null, needs_token: false,
  });
  const peers: PeersState<ContractBodies> = { hosts: [host(DEMO, "/srv/dev/demo"), host(WRITER, "/srv/dev/writer")], polledAgo: 0, polls: 1, now: NOW };
  const root = createRootRoute({ component: () => <PageOfRuns search={search} /> });
  window.location.hash = "#/";
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <PollDepsContext.Provider value={fake.deps}>
        <PeersContext.Provider value={peers}>
          <RouterProvider router={createRouter({ routeTree: root, history: createHashHistory() })} />
        </PeersContext.Provider>
      </PollDepsContext.Provider>
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
}

test("with several hosts in scope each serves its first page * size rows and the page is cut from their merge by start", async () => {
  const at = (id: number, ticket: string, hour: number) => run({ id, ticket, title: ticket, started_ms: today(hour, 0) });
  const daemons = fleet({ rows: [at(1, "DEMO-1", 10), at(2, "DEMO-2", 8)] }, { rows: [at(3, "WRITER-3", 9), at(4, "WRITER-4", 7)] });
  await mountPage({ state: "all", sort: "started", order: "desc", size: 2, page: 2 }, daemons.fetchImpl);
  expect(daemons.askedOf(DEMO)).toEqual([paged(null, 0, 4)]);
  expect(daemons.askedOf(WRITER)).toEqual([paged(null, 0, 4)]);
  expect(rows().map((row) => row.textContent)).toEqual(["DEMO-2", "WRITER-4"]);
});

test("with several hosts in scope a page past the daemon's 5000-row cap is asked for in batches of at most 5000, up to each host's total", async () => {
  const daemons = fleet({ rows: runs("DEMO", 6000, 5101) }, { rows: [] });
  await mountPage({ state: "all", sort: "started", order: "desc", size: 100, page: 51 }, daemons.fetchImpl);
  expect(daemons.askedOf(DEMO)).toEqual([paged(null, 0, 5000), paged(null, 5000, 100)]);
  expect(daemons.askedOf(WRITER)).toEqual([paged(null, 0, 5000)]);
  const shown = rows().map((row) => row.textContent);
  expect(shown).toHaveLength(100);
  expect([shown[0], shown.at(-1)]).toEqual(["DEMO-1000", "DEMO-901"]);
});

test("a sort head puts its sort and order in the hash, asks the hosts with them and names the list, State starts ascending, and newest first drops both", async () => {
  const daemons = fleet({ rows: runs("DEMO", 100, 60) }, { rows: [] });
  await mount("#/runs", daemons.fetchImpl);
  await press(button("Next"));
  expect(hashSearch().get("page")).toBe("2");

  await press(button("Sort by took"));
  expect(hashSearch().get("sort")).toBe("duration");
  expect(hashSearch().get("order")).toBe("desc");
  expect(hashSearch().has("page")).toBe(false);
  expect(daemons.askedOf(DEMO).at(-1)).toEqual(paged(null, 0, 50, "duration", "desc"));
  expect(daemons.askedOf(WRITER).at(-1)).toEqual(paged(null, 0, 50, "duration", "desc"));
  expect(list().getAttribute("aria-label")).toBe("Runs, longest first");
  expect(button("Sort by took").getAttribute("aria-pressed")).toBe("true");

  await press(button("Sort by took"));
  expect(hashSearch().get("order")).toBe("asc");
  expect(list().getAttribute("aria-label")).toBe("Runs, shortest first");

  await press(button("Sort by state"));
  expect(hashSearch().get("sort")).toBe("state");
  expect(hashSearch().get("order")).toBe("asc");

  await press(button("Sort by started"));
  expect(hashSearch().has("sort")).toBe(false);
  expect(hashSearch().has("order")).toBe(false);
  expect(list().getAttribute("aria-label")).toBe("Runs, newest first");
});

test("sorted by took, two hosts' rows are drawn longest first, then shortest first after a second press", async () => {
  const took = (id: number, ticket: string, hour: number, minutes: number) =>
    run({ id, ticket, title: ticket, started_ms: today(hour, 0), elapsed_ms: minutes * MINUTE });
  const longestFirst = (rows: HistoryRow[]) => (params: URLSearchParams) => (params.get("order") === "asc" ? [...rows].reverse() : rows);
  const daemons = fleet(
    { rows: longestFirst([took(1, "DEMO-1", 9, 120), took(2, "DEMO-2", 11, 40)]) },
    { rows: longestFirst([took(3, "WRITER-3", 12, 90), took(4, "WRITER-4", 10, 10)]) },
  );
  await mount("#/runs", daemons.fetchImpl);

  await press(button("Sort by took"));
  expect(shownIds()).toEqual(["1", "3", "2", "4"]);

  await press(button("Sort by took"));
  expect(shownIds()).toEqual(["4", "2", "3", "1"]);
});

test("the State boxes and the count line sum the hosts' state counts and totals, and checking a State box asks the hosts for its group", async () => {
  const daemons = fleet(
    { rows: runs("DEMO", 100, 6), total: 6, counts: { active: 1, merged: 3, failed: 2, abandoned: 0 } },
    { rows: runs("WRITER", 200, 3), total: 3, counts: { active: 0, merged: 1, failed: 0, abandoned: 2 } },
  );
  await mount("#/runs?state=all", daemons.fetchImpl);
  await press(screen.getByRole("button", { name: /^Filter/ }));
  const group = screen.getByRole("group", { name: "State" });
  const boxes = () => within(group).getAllByRole("checkbox").slice(1) as HTMLInputElement[];
  expect(boxes().map((each) => each.closest("label")!.textContent)).toEqual(["Active 1", "Merged 4", "Failed 2", "Abandoned 2"]);
  expect(document.querySelector("[data-count-line]")!.textContent).toStartWith("all projects · 9 runs / 1 active");

  await press(within(group).getByRole("checkbox", { name: "Failed" }));
  expect(hashSearch().get("state")).toBe("failed");
  expect(hashSearch().has("page")).toBe(false);
  expect(daemons.askedOf(DEMO).at(-1)).toEqual(paged("failed", 0, 50));
  expect(daemons.askedOf(WRITER).at(-1)).toEqual(paged("failed", 0, 50));
  expect(boxes().filter((each) => each.checked).map((each) => each.getAttribute("aria-label"))).toEqual(["Failed"]);
});

test("the controls row hides after 24px of scrolling down once the dock is stuck and shows after 24px up, ignoring jitter and the 150ms after a toggle", async () => {
  const clock = await mount("#/runs", fleet({ rows: runs("DEMO", 100, 30) }, { rows: runs("WRITER", 200, 30) }).fetchImpl);
  expect(rows()).toHaveLength(50);
  const dock = document.querySelector<HTMLElement>("[data-runs-dock]")!;
  const controls = dock.querySelector<HTMLElement>("[data-controls]")!;
  expect(within(dock).getAllByRole("button", { name: /^Sort by / })).toHaveLength(5);
  expect(dock.contains(list())).toBe(false);
  Object.defineProperty(controls, "offsetHeight", { configurable: true, value: 52 });
  Object.defineProperty(dock, "offsetHeight", { configurable: true, value: 96 });
  const main = document.querySelector("main")!;
  const scrollTo = async (top: number) => {
    main.scrollTop = top;
    await act(async () => void fireEvent.scroll(main));
  };
  const scrollBy = (step: number) => scrollTo(main.scrollTop + step);
  const snapshot = () => Array.from(dock.querySelectorAll("*"), (element, index) =>
    Array.from(element.attributes, (attribute) => `${index} ${attribute.name}=${attribute.value}`)).flat().concat(`dock style=${dock.getAttribute("style")}`);
  const changed = (before: string[], after: string[]) => [...after.filter((each) => !before.includes(each)), ...before.filter((each) => !after.includes(each))]
    .map((each) => each.replace(/=.*$/, "").replace(/^\d+ /, "")).sort();
  const isHidden = () => controls.hasAttribute("data-hidden");
  const noLayoutShift = () => {
    for (const element of [dock, ...dock.querySelectorAll<HTMLElement>("*")]) {
      expect([element.style.height, element.style.maxHeight, element.style.marginTop]).toEqual(["", "", ""]);
      expect(element.hasAttribute("hidden")).toBe(false);
    }
  };

  const rowsBox = dock.nextElementSibling as HTMLElement;
  const rowsAt = (top: number) => (rowsBox.getBoundingClientRect = () => ({ top }) as DOMRect);
  rowsAt(60);
  await scrollTo(300);
  await scrollBy(30);
  expect(isHidden()).toBe(false);
  rowsAt(0);

  await scrollTo(1000);
  clock.now += 200;
  await scrollBy(-30);
  expect(isHidden()).toBe(false);
  clock.now += 200;

  for (let round = 0; round < 4; round += 1) {
    await scrollBy(10);
    await scrollBy(-10);
  }
  expect(isHidden()).toBe(false);

  const shown = snapshot();
  await scrollBy(10);
  await scrollBy(10);
  await scrollBy(10);
  expect(isHidden()).toBe(true);
  expect(controls.hasAttribute("inert")).toBe(true);
  expect(dock.style.transform).toBe("translateY(-52px)");
  expect(changed(shown, snapshot())).toEqual(["data-hidden", "dock style", "dock style", "inert"]);
  noLayoutShift();

  clock.now += 100;
  await scrollBy(-10);
  expect(isHidden()).toBe(true);
  await scrollBy(-30);
  expect(isHidden()).toBe(true);

  clock.now += 100;
  const hidden = snapshot();
  await scrollBy(-30);
  expect(isHidden()).toBe(false);
  expect(controls.hasAttribute("inert")).toBe(false);
  expect(changed(hidden, snapshot())).toEqual(["data-hidden", "dock style", "dock style", "inert"]);
  noLayoutShift();

  clock.now += 200;
  await scrollBy(10);
  await scrollBy(10);
  await scrollBy(10);
  expect(isHidden()).toBe(true);
  await scrollTo(0);
  expect(isHidden()).toBe(false);
  expect(controls.hasAttribute("inert")).toBe(false);
});

test("unknown sort, order, page and size open newest first on page 1 at 50 a page, and state=merged still checks Merged", async () => {
  const daemons = fleet({ rows: runs("DEMO", 100, 3) }, { rows: [] });
  await mount("#/runs?sort=size&order=up&page=0&size=7", daemons.fetchImpl);
  expect(daemons.askedOf(DEMO)).toEqual([paged(null, 0, 50)]);
  expect(daemons.askedOf(WRITER)).toEqual([paged(null, 0, 50)]);
  expect(list().getAttribute("aria-label")).toBe("Runs, newest first");
  expect((screen.getByRole("combobox", { name: "Rows a page" }) as HTMLSelectElement).value).toBe("50");
  expect(range()).toBe("1–3 of 3");
  cleanup();

  await mount("#/runs?state=merged", daemons.fetchImpl);
  await press(screen.getByRole("button", { name: /^Filter/ }));
  expect((within(screen.getByRole("group", { name: "State" })).getByRole("checkbox", { name: "Merged" }) as HTMLInputElement).checked).toBe(true);
});

test("a scope with no runs says so and draws no pager", async () => {
  await mount("#/runs?project=demo", fleet({ rows: [] }, { rows: runs("WRITER", 200, 3) }).fetchImpl);
  expect(screen.getByText("No runs in demo in the last 7 days.")).toBeTruthy();
  expect(pager()).toBeNull();
});

test("a page past the last one goes back to the last page with runs, and an empty scope drops the page and draws no pager", async () => {
  const daemons = fleet({ rows: runs("DEMO", 120, 120) }, { rows: [] });
  await mount("#/runs?project=demo&state=all&page=5", daemons.fetchImpl);
  expect(hashSearch().get("page")).toBe("3");
  expect(daemons.askedOf(DEMO).at(-1)).toEqual(paged(null, 100, 50));
  expect(range()).toBe("101–120 of 120");
  cleanup();

  await mount("#/runs?project=demo&page=2", fleet({ rows: [] }, { rows: [] }).fetchImpl);
  expect(hashSearch().has("page")).toBe(false);
  expect(screen.getByText("No runs in demo in the last 7 days.")).toBeTruthy();
  expect(pager()).toBeNull();
});

test("a page beyond the fleet's runs asks each host for one batch and goes back to the last page", async () => {
  const daemons = fleet({ rows: runs("DEMO", 100, 3) }, { rows: runs("WRITER", 200, 2) });
  await mount("#/runs?state=all&page=1000000000000", daemons.fetchImpl);
  expect(daemons.askedOf(DEMO)).toEqual([paged(null, 0, 5000), paged(null, 0, 50)]);
  expect(daemons.askedOf(WRITER)).toEqual([paged(null, 0, 5000), paged(null, 0, 50)]);
  expect(hashSearch().has("page")).toBe(false);
  expect(range()).toBe("1–5 of 5");
});

test("Next reaches every page of one host's runs, however deep", async () => {
  const daemons = fleet({ rows: runs("DEMO", 30000, 25001) }, { rows: [] });
  await mount("#/runs?project=demo&state=all&size=25&page=1000", daemons.fetchImpl);
  expect(range()).toBe("24976–25000 of 25001");
  await press(button("Next"));
  expect(hashSearch().get("page")).toBe("1001");
  expect(daemons.askedOf(DEMO).at(-1)).toEqual(paged(null, 25000, 25));
  expect(range()).toBe("25001–25001 of 25001");
  expect(button("Next").hasAttribute("disabled")).toBe(true);
});

test("a page that is not a safe integer opens page 1", async () => {
  const daemons = fleet({ rows: runs("DEMO", 100, 3) }, { rows: runs("WRITER", 200, 2) });
  await mount("#/runs?state=all&page=1e308", daemons.fetchImpl);
  expect(daemons.askedOf(DEMO)).toEqual([paged(null, 0, 50)]);
  expect(daemons.askedOf(WRITER)).toEqual([paged(null, 0, 50)]);
  expect(range()).toBe("1–5 of 5");
});
