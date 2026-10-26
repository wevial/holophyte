import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import type { HistoryCounts, HistoryRow, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const WRITER = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;

const MINUTE = 60_000;
const today = (hour: number, minute: number) => new Date(2026, 9, 15, hour, minute).getTime();
const NOW = today(18, 0);
const midnight = (month: number, date: number) => String(new Date(2026, month - 1, date).getTime());

const run = (row: Partial<HistoryRow> & Pick<HistoryRow, "id" | "ticket" | "title" | "started_ms">): HistoryRow => ({
  ticket_url: null, attempt: 1, phase: "reviewing", outcome: null, outcome_reason: null, failure_kind: null,
  park_kind: null, ended_ms: null, elapsed_ms: 16 * MINUTE, heartbeat_age_ms: 1_000, rounds: 0, max_rounds: 3,
  working_ms: null, estimate_ms: null, pr_url: null, host: null, ...row,
});

const runs = (prefix: string, first: number, count: number) =>
  Array.from({ length: count }, (_, at) => run({ id: first - at, ticket: `${prefix}-${first - at}`, title: `Run ${first - at}`, started_ms: today(17, 59) - at * MINUTE }));

interface Daemon {
  rows: HistoryRow[];
  counts?: Partial<HistoryCounts>;
  implementers?: string[];
}

const NO_COUNTS: HistoryCounts = { state: { active: 0, merged: 0, failed: 0, abandoned: 0 }, failure: {}, implementer: {} };

/** Each daemon answers any `/history` with its rows whose ticket or title holds `q`, cut at `offset` and
 *  `limit`, its `total` counting every row it would answer; every query lands in `asked`. */
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
      const q = params.get("q") ?? "";
      const matching = daemon.rows.filter((row) => row.ticket.includes(q) || row.title.includes(q));
      const offset = Number(params.get("offset") ?? 0);
      const limit = Number(params.get("limit") ?? 200);
      return Response.json({
        rows: matching.slice(offset, offset + limit), since: Number(params.get("since")), until: null, limit, offset,
        truncated: matching.length > offset + limit, total: matching.length,
        counts: { ...NO_COUNTS, ...daemon.counts }, implementers: daemon.implementers ?? [], now: NOW,
      });
    }
    return polls(url);
  };
  const askedOf = (base: string) => asked.filter((each) => each.base === base).map((each) => Object.fromEntries(each.params));
  const lastOf = (base: string) => askedOf(base).at(-1)!;
  return { fetchImpl, askedOf, lastOf };
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
}

const settleAll = async () => {
  for (let round = 0; round < 3; round += 1) await act(settle);
};
const press = async (element: HTMLElement) => {
  await act(async () => {
    fireEvent.click(element);
    await settle();
  });
  await settleAll();
};
const hashSearch = () => new URLSearchParams(window.location.hash.split("?")[1]);
const controls = () => document.querySelector<HTMLElement>("[data-controls]")!;
const countLine = () => document.querySelector("[data-count-line]")!.textContent;
const field = () => screen.getByRole("searchbox", { name: "Search runs" }) as HTMLInputElement;
const filterButton = () => screen.getByRole("button", { name: /^Filter/ });
const started = () => within(screen.getByRole("group", { name: "Started" }));
const pressedRanges = () => started().getAllByRole("button").filter((each) => each.getAttribute("aria-pressed") === "true").map((each) => each.getAttribute("aria-label") ?? each.textContent);
const chips = () => screen.queryAllByRole("button", { name: /^Remove / }).map((each) => each.getAttribute("aria-label")!.slice("Remove ".length));
const dialog = () => screen.queryByRole("dialog", { name: "Filter runs" });
const openFilter = async () => {
  if (!dialog()) await press(filterButton());
  return dialog()!;
};
const section = async (name: string) => within(within(await openFilter()).getByRole("group", { name }));
const valueRows = async (name: string) => (await section(name)).getAllByRole("checkbox").slice(1).map((box) => box.closest("label")!.textContent);
const checkedIn = async (name: string) =>
  ((await section(name)).getAllByRole("checkbox").slice(1) as HTMLInputElement[]).filter((box) => box.checked).map((box) => box.getAttribute("aria-label"));
const head = async (name: string) => (await section(name)).getAllByRole("checkbox")[0] as HTMLInputElement;
const check = async (sectionName: string, value: string) => press((await section(sectionName)).getByRole("checkbox", { name: value }));
const typeSearch = async (text: string) => {
  fireEvent.change(field(), { target: { value: text } });
  await act(() => new Promise((resolve) => setTimeout(resolve, 300)));
  await settleAll();
};

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

test("Runs opens on the last 7 days in no state, its controls row Search, Started, Filter, the count and the page size, with no chip", async () => {
  const daemons = fleet({ rows: runs("DEMO", 100, 3) }, { rows: runs("WRITER", 200, 2) });
  await mount("#/runs", daemons.fetchImpl);
  for (const base of [DEMO, WRITER]) {
    const asked = daemons.lastOf(base);
    expect(asked.since).toBe(midnight(10, 9));
    expect(["state", "failure", "implementer"].filter((name) => name in asked)).toEqual([]);
  }
  const row = controls();
  const order = [
    field(),
    screen.getByRole("group", { name: "Started" }),
    filterButton(),
    within(row).getByText("5 runs"),
    screen.getByRole("combobox", { name: "Rows a page" }),
  ];
  for (const element of order) expect(row.contains(element)).toBe(true);
  for (const [at, element] of order.slice(1).entries()) expect(order[at]!.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(started().getAllByRole("button").map((each) => each.getAttribute("aria-label") ?? each.textContent)).toEqual(["Today", "2d", "7d", "30d", "Any", "Custom range"]);
  expect(pressedRanges()).toEqual(["7d"]);
  expect(filterButton().textContent).toBe("Filter");
  expect(chips()).toEqual([]);
  expect(Array.from(row.querySelectorAll("*"), (element) => element.textContent!.trim()).filter((text) => text.startsWith("All "))).toEqual([]);
  expect(countLine()).toStartWith("all projects · 5 runs");
});

test("Search writes q after its debounce and asks the hosts with it, Escape clears it, and / focuses it", async () => {
  const daemons = fleet({ rows: runs("DEMO", 100, 3) }, { rows: [] });
  await mount("#/runs", daemons.fetchImpl);
  await typeSearch("lock");
  expect(hashSearch().get("q")).toBe("lock");
  expect(daemons.lastOf(DEMO).q).toBe("lock");
  expect(daemons.lastOf(WRITER).q).toBe("lock");
  expect(chips()).toEqual(["Search: “lock”"]);

  await act(async () => void fireEvent.keyDown(field(), { key: "Escape" }));
  await settleAll();
  expect(hashSearch().has("q")).toBe(false);
  expect(chips()).toEqual([]);

  (document.activeElement as HTMLElement | null)?.blur();
  await act(() => userEvent.keyboard("/"));
  expect(document.activeElement).toBe(field());
  expect(field().value).toBe("");
});

test("typing a search from a later page goes back to page 1", async () => {
  const locks = runs("DEMO", 100, 60).map((row) => ({ ...row, title: `Fix the lock ${row.id}` }));
  const daemons = fleet({ rows: locks }, { rows: [] });
  await mount("#/runs?page=2", daemons.fetchImpl);
  expect(daemons.lastOf(DEMO).limit).toBe("100");
  await typeSearch("lock");
  expect(hashSearch().get("q")).toBe("lock");
  expect(hashSearch().has("page")).toBe(false);
  expect(daemons.lastOf(DEMO).limit).toBe("50");
});

test("Escape and a press outside close the Filter popover and give focus back to its button", async () => {
  await mount("#/runs", countedFleet().fetchImpl);
  await check("State", "Failed");
  expect(document.activeElement).not.toBe(filterButton());
  await act(async () => void fireEvent.keyDown(document.activeElement!, { key: "Escape" }));
  expect(dialog()).toBeNull();
  expect(filterButton().getAttribute("aria-expanded")).toBe("false");
  expect(document.activeElement).toBe(filterButton());

  await openFilter();
  (await section("State")).getByRole("checkbox", { name: "Merged" }).focus();
  await act(async () => void fireEvent.pointerDown(document.querySelector("[data-count-line]")!));
  expect(dialog()).toBeNull();
  expect(document.activeElement).toBe(filterButton());
});

test("a Started button asks the hosts from its day and a custom range from its From day until the day after its To", async () => {
  const daemons = fleet({ rows: runs("DEMO", 100, 3) }, { rows: [] });
  await mount("#/runs", daemons.fetchImpl);
  await press(started().getByRole("button", { name: "30d" }));
  expect(hashSearch().get("range")).toBe("30d");
  expect(daemons.lastOf(DEMO).since).toBe(midnight(9, 16));
  expect(daemons.lastOf(WRITER).since).toBe(midnight(9, 16));
  expect(pressedRanges()).toEqual(["30d"]);
  expect(chips()).toEqual(["Started: Last 30 days"]);

  await press(started().getByRole("button", { name: "Today" }));
  expect(hashSearch().get("range")).toBe("today");
  expect(daemons.lastOf(DEMO).since).toBe(midnight(10, 15));

  await press(started().getByRole("button", { name: "Custom range" }));
  const custom = within(screen.getByRole("group", { name: "Started range" }));
  for (const [name, value] of [["From", "2026-10-01"], ["To", "2026-10-20"]] as const) {
    await act(async () => void fireEvent.change(custom.getByLabelText(name), { target: { value } }));
    await settleAll();
  }
  expect(hashSearch().get("from")).toBe("2026-10-01");
  expect(hashSearch().get("to")).toBe("2026-10-20");
  expect(hashSearch().has("range")).toBe(false);
  for (const base of [DEMO, WRITER]) {
    expect(daemons.lastOf(base).since).toBe(midnight(10, 1));
    expect(daemons.lastOf(base).until).toBe(midnight(10, 21));
  }
  const customButton = started().getByRole("button", { name: "Custom range" });
  expect(pressedRanges()).toEqual(["Custom range"]);
  expect(customButton.textContent).toBe("Oct 1 – Oct 20");
  expect(chips()).toEqual(["Started: Oct 1 – Oct 20"]);
});

const countedFleet = (demoRows = 6) => fleet(
  {
    rows: runs("DEMO", 100, demoRows),
    counts: { state: { active: 1, merged: 3, failed: 2, abandoned: 0 }, failure: { verify: 1, review_route: 1 }, implementer: { "claude opus": 4 } },
    implementers: ["claude opus"],
  },
  {
    rows: runs("WRITER", 200, 3),
    counts: { state: { active: 0, merged: 1, failed: 0, abandoned: 2 }, failure: { verify: 2 }, implementer: { "claude opus": 1, "codex gpt-6-astra": 2 } },
    implementers: ["claude opus", "codex gpt-6-astra"],
  },
);

test("the Filter popover holds Project, State, Failure kind and Implementer with the hosts' summed counts, and closes when the row hides", async () => {
  await mount("#/runs", countedFleet().fetchImpl);
  const box = await openFilter();
  expect(filterButton().getAttribute("aria-expanded")).toBe("true");
  expect(within(box).getAllByRole("group")).toEqual(["Project", "State", "Failure kind", "Implementer"].map((name) => within(box).getByRole("group", { name })));
  expect((within(box).getAllByRole("checkbox") as HTMLInputElement[]).filter((each) => each.checked || each.indeterminate)).toEqual([]);
  expect(await valueRows("State")).toEqual(["Active 1", "Merged 4", "Failed 2", "Abandoned 2"]);
  expect((await valueRows("Failure kind")).slice(0, 2)).toEqual(["verify 3", "review 1"]);
  expect((await section("Implementer")).getAllByRole("checkbox").slice(1).map((each) => each.getAttribute("aria-label"))).toEqual(["claude opus", "codex gpt-6-astra"]);

  const main = document.querySelector("main")!;
  main.scrollTop = 100;
  await act(async () => void fireEvent.scroll(main));
  expect(dialog()).toBeNull();
  expect(controls().hasAttribute("data-hidden")).toBe(true);
});

test("checking State and Failure kind boxes writes them as comma lists in section order, and the State head checks all, then none", async () => {
  const daemons = countedFleet(60);
  await mount("#/runs?page=2", daemons.fetchImpl);
  expect(hashSearch().get("page")).toBe("2");
  await check("State", "Failed");
  await check("Failure kind", "verify");
  await check("Failure kind", "review");
  expect(hashSearch().get("state")).toBe("failed");
  expect(hashSearch().get("failure")).toBe("verify,review_route");
  expect(hashSearch().has("page")).toBe(false);
  for (const base of [DEMO, WRITER]) {
    expect(daemons.lastOf(base).state).toBe("failed");
    expect(daemons.lastOf(base).failure).toBe("verify,review_route");
  }
  expect(filterButton().textContent).toBe("Filter · 3");
  const stateHead = await head("State");
  expect([stateHead.checked, stateHead.indeterminate]).toEqual([false, true]);
  expect(stateHead.closest("label")!.textContent).toContain("1 of 4");
  expect(chips()).toEqual(["State: Failed", "Failure: verify, review"]);

  await press(await head("State"));
  expect(await checkedIn("State")).toEqual(["Active", "Merged", "Failed", "Abandoned"]);
  expect([(await head("State")).checked, (await head("State")).indeterminate]).toEqual([true, false]);
  expect(hashSearch().get("state")).toBe("active,merged,failed,abandoned");

  await press(await head("State"));
  expect(await checkedIn("State")).toEqual([]);
  expect(hashSearch().has("state")).toBe(false);
});

test("checking a Project draws only its rows and asks the other host for one row, leaving the scope, and Implementer asks with both labels", async () => {
  const daemons = countedFleet();
  await mount("#/runs", daemons.fetchImpl);
  await check("Project", "demo");
  expect(hashSearch().get("projects")).toBe("demo");
  expect(hashSearch().has("project")).toBe(false);
  const shown = Array.from(document.querySelectorAll("ol[aria-label^='Runs, '] > li"), (row) => row.textContent);
  expect(shown).toHaveLength(6);
  expect(shown.every((text) => text!.includes("DEMO-"))).toBe(true);
  expect(daemons.lastOf(WRITER).limit).toBe("1");
  expect(await valueRows("Project")).toEqual(["demo 6", "writer 3"]);
  expect(await valueRows("State")).toEqual(["Active 1", "Merged 4", "Failed 2", "Abandoned 2"]);
  expect((await valueRows("Failure kind")).slice(0, 1)).toEqual(["verify 3"]);
  expect(await valueRows("Implementer")).toEqual(["claude opus 5", "codex gpt-6-astra 2"]);
  expect(countLine()).toStartWith("demo · ");
  expect(chips()).toEqual(["Project: demo"]);
  expect(sessionStorage.getItem("holophyte.next.project")).toBeNull();

  await check("Implementer", "claude opus");
  await check("Implementer", "codex gpt-6-astra");
  for (const base of [DEMO, WRITER]) expect(daemons.lastOf(base).implementer).toBe("claude opus,codex gpt-6-astra");
  expect(chips()).toContain("Implementer: claude opus, codex gpt-6-astra");
});

test("a link's filters show in every control and chip and reach the hosts; a chip's Remove drops its filter and Clear filters keeps sort and size", async () => {
  const daemons = countedFleet();
  await mount("#/runs?q=lock&projects=demo&state=failed,abandoned&failure=verify&range=30d&sort=duration&order=desc&size=25", daemons.fetchImpl);
  expect(field().value).toBe("lock");
  expect(pressedRanges()).toEqual(["30d"]);
  expect(await checkedIn("Project")).toEqual(["demo"]);
  expect(await checkedIn("State")).toEqual(["Failed", "Abandoned"]);
  expect(await checkedIn("Failure kind")).toEqual(["verify"]);
  expect(chips()).toEqual(["Search: “lock”", "Project: demo", "State: Failed, Abandoned", "Failure: verify", "Started: Last 30 days"]);
  expect(daemons.lastOf(DEMO)).toMatchObject({ q: "lock", state: "failed,abandoned", failure: "verify", since: midnight(9, 16), sort: "duration", order: "desc", limit: "25" });

  await press(screen.getByRole("button", { name: "Remove Failure: verify" }));
  expect(hashSearch().has("failure")).toBe(false);
  expect(Object.fromEntries(hashSearch())).toMatchObject({ q: "lock", projects: "demo", state: "failed,abandoned", range: "30d", sort: "duration", order: "desc", size: "25" });

  await press(screen.getByRole("button", { name: "Clear filters" }));
  for (const name of ["q", "projects", "state", "failure", "implementer", "range", "from", "to"]) expect(hashSearch().has(name)).toBe(false);
  expect(Object.fromEntries(hashSearch())).toMatchObject({ sort: "duration", order: "desc", size: "25" });
  expect(filterButton().textContent).toBe("Filter");
  expect(chips()).toEqual([]);
});

test("an Implementer the hosts do not list is dropped from the query, the boxes and the chips, and a listed one is kept", async () => {
  const daemons = countedFleet();
  await mount("#/runs?implementer=nope,claude%20opus", daemons.fetchImpl);
  for (const base of [DEMO, WRITER]) expect(daemons.lastOf(base).implementer).toBe("claude opus");
  expect((await section("Implementer")).getAllByRole("checkbox").slice(1).map((each) => each.getAttribute("aria-label"))).toEqual(["claude opus", "codex gpt-6-astra"]);
  expect(await checkedIn("Implementer")).toEqual(["claude opus"]);
  expect(chips()).toEqual(["Implementer: claude opus"]);
  expect(filterButton().textContent).toBe("Filter · 1");
});

test("a filter no run matches reads No runs match once with no pager, and an empty scope with no filter names the project and the 7 days", async () => {
  await mount("#/runs?q=nothing", fleet({ rows: runs("DEMO", 100, 3) }, { rows: [] }).fetchImpl);
  expect(screen.getAllByText("No runs match")).toHaveLength(1);
  const heads = document.querySelector("[data-run-heads]")!;
  expect(heads.compareDocumentPosition(screen.getByText("No runs match")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(screen.queryByRole("navigation", { name: "Pages of runs" })).toBeNull();
  cleanup();

  await mount("#/runs?project=demo", fleet({ rows: [] }, { rows: runs("WRITER", 200, 3) }).fetchImpl);
  expect(screen.getByText("No runs in demo in the last 7 days.")).toBeTruthy();
});

for (const [hash, checked, asked] of [
  ["state=all", [], {}],
  ["state=failed", ["Failed"], { state: "failed" }],
  ["state=failed,nope&failure=verify,nope", ["Failed"], { state: "failed", failure: "verify" }],
] as const) {
  test(`an old or partly unknown link, ${hash}, checks only the values it knows and asks the hosts with them`, async () => {
    const daemons = countedFleet();
    await mount(`#/runs?${hash}`, daemons.fetchImpl);
    expect(await checkedIn("State")).toEqual([...checked]);
    for (const base of [DEMO, WRITER]) {
      const { state, failure } = daemons.lastOf(base);
      expect({ ...(state ? { state } : {}), ...(failure ? { failure } : {}) }).toEqual(asked);
    }
  });
}
