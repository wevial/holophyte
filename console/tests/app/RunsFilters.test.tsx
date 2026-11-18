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

/** A run's state group as the daemon files it: an open run is active, an ended one by its outcome. */
const groupOf = (row: HistoryRow) => (row.outcome == null ? "active" : row.outcome === "merged" || row.outcome === "abandoned" ? row.outcome : "failed");

/** Each daemon answers any `/history` with its rows whose ticket or title holds `q` and whose group is in `state`, cut at `offset` and
 *  `limit`, its `total` counting every row it would answer; every query lands in `asked`. */
function fleet(demo: Daemon, writer: Daemon, writerPath = "/srv/dev/writer") {
  const asked: { base: string; params: URLSearchParams }[] = [];
  const bodies = (project: string) => ({
    status: { ...working, project, runs: [], queued: [] },
    attention: { level: "none", now: NOW, project, items: [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: bodies("/srv/dev/demo"), [WRITER]: bodies(writerPath) });
  const fetchImpl: Fetch = async (url) => {
    for (const [base, daemon] of [[DEMO, demo], [WRITER, writer]] as const) {
      if (!url.startsWith(`${base}/history?`)) continue;
      const params = new URL(url).searchParams;
      asked.push({ base, params });
      const q = params.get("q") ?? "";
      const states = params.get("state")?.split(",");
      const matching = daemon.rows.filter((row) => (row.ticket.includes(q) || row.title.includes(q)) && (states == null || states.includes(groupOf(row))));
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
const field = () => screen.getByRole("searchbox", { name: "Search runs" }) as HTMLInputElement;
const searchButton = () => screen.queryByRole("button", { name: "Search runs" });
const openSearch = async () => {
  if (searchButton()) await press(searchButton()!);
  return field();
};
const filterButton = () => screen.getByRole("button", { name: /^Filter and sort/ });
const filterDot = () => filterButton().querySelector("[data-filter-dot]");
const rangeButton = () => screen.getByRole("button", { name: /^Range: / });
const rangeDialog = () => screen.queryByRole("dialog", { name: "Range" });
const openRange = async () => {
  if (!rangeDialog()) await press(rangeButton());
  return within(rangeDialog()!);
};
const checkedRanges = async () => (await openRange()).getAllByRole("radio").filter((each) => each.getAttribute("aria-checked") === "true").map((each) => each.textContent);
const chips = () => screen.queryAllByRole("button", { name: /^Remove / }).map((each) => each.getAttribute("aria-label")!.slice("Remove ".length));
const dialog = () => screen.queryByRole("dialog", { name: "Filter and sort" });
const openFilter = async () => {
  if (!dialog()) await press(filterButton());
  return dialog()!;
};
const section = async (name: string) => within(within(await openFilter()).getByRole("group", { name }));
/** A section's option boxes, without the heading's box named for the section. */
const optionBoxes = async (name: string) => ((await section(name)).getAllByRole("checkbox") as HTMLInputElement[]).filter((box) => box.getAttribute("aria-label") !== name);
const valueRows = async (name: string) => (await optionBoxes(name)).map((box) => box.closest("label")!.textContent);
const checkedIn = async (name: string) => (await optionBoxes(name)).filter((box) => box.checked).map((box) => box.getAttribute("aria-label"));
const check = async (sectionName: string, value: string) => press((await section(sectionName)).getByRole("checkbox", { name: value }));
const tabs = () => within(screen.getByRole("group", { name: "Run states" })).getAllByRole("button");
const tabTexts = () => tabs().map((tab) => tab.textContent);
const pressedTabs = () => tabs().filter((tab) => tab.getAttribute("aria-pressed") === "true").map((tab) => tab.textContent!.split(" ")[0]);
const shownIds = () => Array.from(document.querySelectorAll("li[data-run]"), (row) => row.getAttribute("data-run"));
const typeSearch = async (text: string) => {
  fireEvent.change(await openSearch(), { target: { value: text } });
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

test("Runs opens on the last 7 days in no state, its header the title then Search runs, Range: 7 days and Filter and sort, with no count line, run count, page size or chip", async () => {
  const daemons = fleet({ rows: runs("DEMO", 100, 3) }, { rows: runs("WRITER", 200, 2) });
  await mount("#/runs", daemons.fetchImpl);
  for (const base of [DEMO, WRITER]) {
    const asked = daemons.lastOf(base);
    expect(asked.since).toBe(midnight(10, 9));
    expect(["state", "failure", "implementer"].filter((name) => name in asked)).toEqual([]);
  }
  const title = screen.getByRole("heading", { level: 1, name: "Runs" });
  const header = title.closest("header")!;
  const tools = within(header).getAllByRole("button").filter((each) => each.hasAttribute("aria-label"));
  expect(tools.map((each) => each.getAttribute("aria-label"))).toEqual(["Search runs", "Range: 7 days", "Filter and sort"]);
  for (const tool of tools) expect(title.compareDocumentPosition(tool) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(rangeButton().textContent).toBe("7d");
  expect(filterDot()).toBeNull();
  expect(document.querySelector("[data-count-line]")).toBeNull();
  expect(screen.queryAllByText(/\b\d+ runs\b/)).toEqual([]);
  expect(screen.queryByRole("combobox", { name: "Rows a page" })).toBeNull();
  expect(chips()).toEqual([]);
});

test("Search runs opens a focused field writing q after its debounce, Escape clears it, leaving it empty folds it back, and / opens it", async () => {
  const daemons = fleet({ rows: runs("DEMO", 100, 3) }, { rows: [] });
  await mount("#/runs", daemons.fetchImpl);
  expect(screen.queryByRole("searchbox")).toBeNull();
  await press(searchButton()!);
  expect(searchButton()).toBeNull();
  expect(document.activeElement).toBe(field());

  fireEvent.change(field(), { target: { value: "lock" } });
  await settleAll();
  expect(hashSearch().has("q")).toBe(false);
  await act(() => new Promise((resolve) => setTimeout(resolve, 300)));
  await settleAll();
  expect(hashSearch().get("q")).toBe("lock");
  expect(daemons.lastOf(DEMO).q).toBe("lock");
  expect(daemons.lastOf(WRITER).q).toBe("lock");
  expect(chips()).toEqual(["Search: “lock”"]);

  await act(async () => void fireEvent.keyDown(field(), { key: "Escape" }));
  await settleAll();
  expect(hashSearch().has("q")).toBe(false);
  expect(chips()).toEqual([]);
  expect(document.activeElement).toBe(field());

  await act(async () => field().blur());
  expect(screen.queryByRole("searchbox")).toBeNull();
  expect(searchButton()).not.toBeNull();

  await act(() => userEvent.keyboard("/"));
  expect(document.activeElement).toBe(field());
  expect(field().value).toBe("");
});

test("Escape in a search field the link opened clears q and gives focus to the Search runs button", async () => {
  await mount("#/runs?q=lock", fleet({ rows: runs("DEMO", 100, 3) }, { rows: [] }).fetchImpl);
  field().focus();
  await act(async () => void fireEvent.keyDown(field(), { key: "Escape" }));
  await settleAll();
  expect(hashSearch().has("q")).toBe(false);
  expect(screen.queryByRole("searchbox")).toBeNull();
  expect(document.activeElement).toBe(searchButton());
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
  await check("Failure kind", "verify");
  expect(document.activeElement).not.toBe(filterButton());
  await act(async () => void fireEvent.keyDown(document.activeElement!, { key: "Escape" }));
  expect(dialog()).toBeNull();
  expect(filterButton().getAttribute("aria-expanded")).toBe("false");
  expect(document.activeElement).toBe(filterButton());

  await openFilter();
  (await section("Failure kind")).getByRole("checkbox", { name: "review" }).focus();
  await act(async () => void fireEvent.pointerDown(screen.getByRole("heading", { level: 1, name: "Runs" })));
  expect(dialog()).toBeNull();
  expect(document.activeElement).toBe(filterButton());

  await openFilter();
  let pressOnSearch: boolean | undefined;
  await act(async () => void (pressOnSearch = fireEvent.pointerDown(searchButton()!)));
  expect(dialog()).toBeNull();
  expect(pressOnSearch).toBe(false);
  expect(document.activeElement).toBe(filterButton());
});

test("a run started before yesterday names its date with its clock, and yesterday's keeps yest", async () => {
  const older = new Date(2026, 9, 9, 10, 30).getTime();
  const yesterday = new Date(2026, 9, 14, 21, 5).getTime();
  const rows = [
    run({ id: 2, ticket: "DEMO-2", title: "Last night", started_ms: yesterday }),
    run({ id: 1, ticket: "DEMO-1", title: "Last week", started_ms: older, ended_ms: older + 30 * MINUTE, outcome: "merged", phase: "merged" }),
  ];
  await mount("#/runs", fleet({ rows }, { rows: [] }).fetchImpl);
  const label = (id: number) => screen.getByRole("link", { name: new RegExp(`^Run ${id}, `) }).getAttribute("aria-label");
  expect(label(1)).toEndWith("started Oct 9 10:30");
  expect(label(2)).toEndWith("started yest 21:05");
});

test("Escape and a press outside close the Range popover and give focus back to its button", async () => {
  await mount("#/runs", countedFleet().fetchImpl);
  (await openRange()).getByLabelText("From").focus();
  await act(async () => void fireEvent.keyDown(document.activeElement!, { key: "Escape" }));
  expect(rangeDialog()).toBeNull();
  expect(rangeButton().getAttribute("aria-expanded")).toBe("false");
  expect(document.activeElement).toBe(rangeButton());

  (await openRange()).getByRole("radio", { name: "Today" }).focus();
  await act(async () => void fireEvent.pointerDown(screen.getByRole("heading", { level: 1, name: "Runs" })));
  expect(rangeDialog()).toBeNull();
  expect(document.activeElement).toBe(rangeButton());
  expect(hashSearch().has("range")).toBe(false);
});

test("Escape with focus moved from the Filter popover to Search still closes it and gives focus back to its button", async () => {
  await mount("#/runs?q=lock", countedFleet().fetchImpl);
  await openFilter();
  field().focus();
  await act(async () => void fireEvent.keyDown(field(), { key: "Escape" }));
  expect(dialog()).toBeNull();
  expect(document.activeElement).toBe(filterButton());
});

test("Clear filters empties Search and drops a search still waiting on its debounce", async () => {
  const daemons = fleet({ rows: runs("DEMO", 100, 3) }, { rows: [] });
  await mount("#/runs?failure=verify", daemons.fetchImpl);
  fireEvent.change(await openSearch(), { target: { value: "lock" } });
  await press(screen.getByRole("button", { name: "Clear filters" }));
  await act(() => new Promise((resolve) => setTimeout(resolve, 300)));
  await settleAll();
  expect(hashSearch().has("q")).toBe(false);
  expect(screen.queryByRole("searchbox")).toBeNull();
  expect(daemons.lastOf(DEMO).q).toBeUndefined();
  expect(chips()).toEqual([]);
  expect((await openSearch()).value).toBe("");
});

test("the Range dialog lists its presets with 7 days checked, and 30 days writes range=30d, closes it, relabels the button and asks the hosts from 30 days back", async () => {
  const daemons = fleet({ rows: runs("DEMO", 100, 3) }, { rows: [] });
  await mount("#/runs", daemons.fetchImpl);
  const box = await openRange();
  expect(rangeButton().getAttribute("aria-expanded")).toBe("true");
  expect(box.getAllByRole("radio").map((each) => each.textContent)).toEqual(["Today", "2 days", "7 days", "30 days", "Any time"]);
  expect(await checkedRanges()).toEqual(["7 days"]);

  await press(box.getByRole("radio", { name: "30 days" }));
  expect(hashSearch().get("range")).toBe("30d");
  expect(rangeDialog()).toBeNull();
  expect(document.activeElement).toBe(rangeButton());
  expect(rangeButton().getAttribute("aria-label")).toBe("Range: 30 days");
  expect(rangeButton().textContent).toBe("30d");
  for (const base of [DEMO, WRITER]) expect(daemons.lastOf(base).since).toBe(midnight(9, 16));
  expect(chips()).toEqual(["Started: Last 30 days"]);

  await press((await openRange()).getByRole("radio", { name: "Today" }));
  expect(hashSearch().get("range")).toBe("today");
  expect(rangeButton().textContent).toBe("Today");
  expect(daemons.lastOf(DEMO).since).toBe(midnight(10, 15));
});

test("From and To picked in the Range dialog show on its button and ask the hosts from the From day until the day after the To day", async () => {
  const daemons = fleet({ rows: runs("DEMO", 100, 3) }, { rows: [] });
  await mount("#/runs?range=30d", daemons.fetchImpl);
  for (const [name, value] of [["From", "2026-10-01"], ["To", "2026-10-20"]] as const) {
    await act(async () => void fireEvent.change((await openRange()).getByLabelText(name), { target: { value } }));
    await settleAll();
  }
  expect(hashSearch().get("from")).toBe("2026-10-01");
  expect(hashSearch().get("to")).toBe("2026-10-20");
  expect(hashSearch().has("range")).toBe(false);
  for (const base of [DEMO, WRITER]) {
    expect(daemons.lastOf(base).since).toBe(midnight(10, 1));
    expect(daemons.lastOf(base).until).toBe(midnight(10, 21));
  }
  expect(await checkedRanges()).toEqual([]);
  expect(rangeButton().textContent).toBe("Oct 1 – Oct 20");
  expect(rangeButton().getAttribute("aria-label")).toBe("Range: Oct 1 – Oct 20");
  expect(chips()).toEqual(["Started: Oct 1 – Oct 20"]);
});

test("the Range dialog's dates show the picked preset's days up to today, and changing To alone keeps that From", async () => {
  const daemons = fleet({ rows: runs("DEMO", 100, 3) }, { rows: [] });
  await mount("#/runs", daemons.fetchImpl);
  const box = await openRange();
  expect([(box.getByLabelText("From") as HTMLInputElement).value, (box.getByLabelText("To") as HTMLInputElement).value]).toEqual(["2026-10-09", "2026-10-15"]);

  await act(async () => void fireEvent.change(box.getByLabelText("To"), { target: { value: "2026-10-12" } }));
  await settleAll();
  expect([hashSearch().get("from"), hashSearch().get("to"), hashSearch().has("range")]).toEqual(["2026-10-09", "2026-10-12", false]);
  expect(daemons.lastOf(DEMO).until).toBe(midnight(10, 13));
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

test("the Filter popover holds Sort, Project, Failure kind and Implementer with the hosts' summed counts, and closes when the row hides", async () => {
  await mount("#/runs", countedFleet().fetchImpl);
  const box = await openFilter();
  expect(filterButton().getAttribute("aria-expanded")).toBe("true");
  expect(within(box).getAllByRole("group")).toEqual(["Sort", "Per page", "Project", "Failure kind", "Implementer"].map((name) => within(box).getByRole("group", { name })));
  expect((within(box).getAllByRole("checkbox") as HTMLInputElement[]).filter((each) => each.checked || each.indeterminate)).toEqual([]);
  expect((await valueRows("Failure kind")).slice(0, 2)).toEqual(["verify 3", "review 1"]);
  expect((await optionBoxes("Implementer")).map((each) => each.getAttribute("aria-label"))).toEqual(["claude opus", "codex gpt-6-astra"]);

  const main = document.querySelector("main")!;
  main.scrollTop = 100;
  await act(async () => void fireEvent.scroll(main));
  expect(dialog()).toBeNull();
  expect(controls().hasAttribute("data-hidden")).toBe(true);
});

test("checking Failure kind boxes writes them as a comma list in the section's order, and unchecking both drops it", async () => {
  const daemons = countedFleet(60);
  await mount("#/runs?page=2", daemons.fetchImpl);
  expect(hashSearch().get("page")).toBe("2");
  await check("Failure kind", "review");
  await check("Failure kind", "verify");
  expect(hashSearch().get("failure")).toBe("verify,review_route");
  expect(hashSearch().has("page")).toBe(false);
  for (const base of [DEMO, WRITER]) expect(daemons.lastOf(base).failure).toBe("verify,review_route");
  expect(filterButton().getAttribute("aria-label")).toBe("Filter and sort, 2 filters on");
  expect(await optionBoxes("Failure kind")).toHaveLength(9);
  expect(chips()).toEqual(["Failure: verify, review"]);

  await check("Failure kind", "verify");
  await check("Failure kind", "review");
  expect(await checkedIn("Failure kind")).toEqual([]);
  expect(hashSearch().has("failure")).toBe(false);
});

test("with a Failure kind and a Project picked, the Failure kind heading's box picks every failure kind and keeps the Project pick", async () => {
  await mount("#/runs?failure=verify&projects=demo", countedFleet().fetchImpl);
  await press((await section("Failure kind")).getByRole("checkbox", { name: "Failure kind" }));
  expect((await optionBoxes("Failure kind")).map((box) => box.checked)).toEqual(Array(9).fill(true));
  expect(hashSearch().get("failure")!.split(",")).toHaveLength(9);
  expect(await checkedIn("Project")).toEqual(["demo"]);
  expect(hashSearch().get("projects")).toBe("demo");
});

test("each state tab shows its group's count summed over the hosts and All their sum; Failed writes state=failed and lists only failed runs", async () => {
  const ended = (outcome: string) => ({ phase: "done", outcome, ended_ms: today(17, 0) });
  const demoRows = [
    run({ id: 5, ticket: "DEMO-5", title: "Draft", started_ms: today(17, 0) }),
    run({ id: 4, ticket: "DEMO-4", title: "Break", started_ms: today(16, 0), ...ended("failed") }),
    run({ id: 3, ticket: "DEMO-3", title: "Ship", started_ms: today(15, 0), ...ended("merged") }),
  ];
  const writerRows = [run({ id: 9, ticket: "WRITER-9", title: "Break too", started_ms: today(16, 30), ...ended("failed") })];
  const daemons = fleet(
    { rows: demoRows, counts: { state: { active: 1, merged: 3, failed: 1, abandoned: 4 } } },
    { rows: writerRows, counts: { state: { active: 0, merged: 0, failed: 1, abandoned: 0 } } },
  );
  await mount("#/runs", daemons.fetchImpl);
  expect(tabTexts()).toEqual(["All 10", "Active 1", "Merged 3", "Failed 2", "Abandoned 4"]);
  expect(pressedTabs()).toEqual(["All"]);

  await press(tabs().find((tab) => tab.textContent!.startsWith("Failed "))!);
  expect(hashSearch().get("state")).toBe("failed");
  for (const base of [DEMO, WRITER]) expect(daemons.lastOf(base).state).toBe("failed");
  expect(pressedTabs()).toEqual(["Failed"]);
  expect(shownIds()).toEqual(["9", "4"]);
});

test("Took picked under Sort in the Filter popover orders the runs longest first with no day headings", async () => {
  const rows = [
    run({ id: 3, ticket: "DEMO-3", title: "Short", started_ms: today(17, 0), elapsed_ms: 5 * MINUTE }),
    run({ id: 2, ticket: "DEMO-2", title: "Long", started_ms: new Date(2026, 9, 14, 9, 0).getTime(), elapsed_ms: 90 * MINUTE }),
    run({ id: 1, ticket: "DEMO-1", title: "Middle", started_ms: new Date(2026, 9, 12, 9, 0).getTime(), elapsed_ms: 30 * MINUTE }),
  ];
  await mount("#/runs", fleet({ rows }, { rows: [] }).fetchImpl);
  expect(document.querySelectorAll("[data-day]")).toHaveLength(3);
  await press((await section("Sort")).getByRole("radio", { name: "Took" }));
  expect(shownIds()).toEqual(["2", "1", "3"]);
  expect(document.querySelectorAll("[data-day]")).toHaveLength(0);
});

test("two hosts serving one project give it one Project box counting both hosts' runs", async () => {
  const daemons = fleet({ rows: runs("DEMO", 100, 3) }, { rows: runs("WRITER", 200, 4) }, "/srv/dev/demo");
  await mount("#/runs", daemons.fetchImpl);
  expect(await valueRows("Project")).toEqual(["demo 7"]);
});

test("checking a Project draws only its rows and asks the other host for one row, leaving the scope, and Implementer asks with both labels", async () => {
  const daemons = countedFleet();
  await mount("#/runs", daemons.fetchImpl);
  await check("Project", "demo");
  expect(hashSearch().get("projects")).toBe("demo");
  expect(hashSearch().has("project")).toBe(false);
  const shown = Array.from(document.querySelectorAll("li[data-run]"), (row) => row.textContent);
  expect(shown).toHaveLength(6);
  expect(shown.every((text) => text!.includes("DEMO-"))).toBe(true);
  expect(daemons.lastOf(WRITER).limit).toBe("1");
  expect(await valueRows("Project")).toEqual(["demo 6", "writer 3"]);
  expect((await valueRows("Failure kind")).slice(0, 1)).toEqual(["verify 3"]);
  expect(await valueRows("Implementer")).toEqual(["claude opus 5", "codex gpt-6-astra 2"]);
  expect(chips()).toEqual(["Project: demo"]);
  expect(sessionStorage.getItem("holophyte.next.project")).toBeNull();

  await check("Implementer", "claude opus");
  await check("Implementer", "codex gpt-6-astra");
  for (const base of [DEMO, WRITER]) expect(daemons.lastOf(base).implementer).toBe("claude opus,codex gpt-6-astra");
  expect(chips()).toContain("Implementer: claude opus, codex gpt-6-astra");
});

test("one Project checked names the Filter button 1 filter on and shows its dot, and none checked shows no dot", async () => {
  await mount("#/runs", countedFleet().fetchImpl);
  await check("Project", "writer");
  expect(filterButton().getAttribute("aria-label")).toBe("Filter and sort, 1 filter on");
  expect(filterDot()).not.toBeNull();
  await check("Project", "writer");
  expect(hashSearch().has("projects")).toBe(false);
  expect(filterButton().getAttribute("aria-label")).toBe("Filter and sort");
  expect(filterDot()).toBeNull();
});

test("a link's filters show in every control and chip and reach the hosts; a chip's Remove drops its filter and Clear filters keeps sort and size", async () => {
  const daemons = countedFleet();
  await mount("#/runs?q=lock&projects=demo&state=failed,abandoned&failure=verify&range=30d&sort=duration&order=desc&size=25", daemons.fetchImpl);
  expect(field().value).toBe("lock");
  expect(rangeButton().textContent).toBe("30d");
  expect(await checkedIn("Project")).toEqual(["demo"]);
  expect(await checkedIn("Failure kind")).toEqual(["verify"]);
  expect((await section("Sort")).getByRole("radio", { name: "Took" })).toHaveProperty("checked", true);
  expect(pressedTabs()).toEqual([]);
  expect(chips()).toEqual(["Search: “lock”", "Project: demo", "Failure: verify", "State: Failed, Abandoned", "Started: Last 30 days"]);
  expect(daemons.lastOf(DEMO)).toMatchObject({ q: "lock", state: "failed,abandoned", failure: "verify", since: midnight(9, 16), sort: "duration", order: "desc", limit: "25" });

  await press(screen.getByRole("button", { name: "Remove Failure: verify" }));
  expect(hashSearch().has("failure")).toBe(false);
  expect(Object.fromEntries(hashSearch())).toMatchObject({ q: "lock", projects: "demo", state: "failed,abandoned", range: "30d", sort: "duration", order: "desc", size: "25" });

  await press(screen.getByRole("button", { name: "Clear filters" }));
  for (const name of ["q", "projects", "state", "failure", "implementer", "range", "from", "to"]) expect(hashSearch().has(name)).toBe(false);
  expect(Object.fromEntries(hashSearch())).toMatchObject({ sort: "duration", order: "desc", size: "25" });
  expect(filterButton().getAttribute("aria-label")).toBe("Filter and sort");
  expect(chips()).toEqual([]);
});

test("Clear in the Filter popover drops every checked filter and keeps the search, range and sort", async () => {
  const daemons = countedFleet();
  await mount("#/runs?q=DEMO&projects=demo&failure=verify&range=30d&sort=duration", daemons.fetchImpl);
  const clear = within(await openFilter()).getByRole("button", { name: "Clear" });
  await press(clear);
  expect(["projects", "failure"].filter((name) => hashSearch().has(name))).toEqual([]);
  expect(Object.fromEntries(hashSearch())).toMatchObject({ q: "DEMO", range: "30d", sort: "duration" });
  expect(daemons.lastOf(DEMO).failure).toBeUndefined();
  expect(filterDot()).toBeNull();
  expect(within(await openFilter()).getByRole("button", { name: "Clear" }).hasAttribute("disabled")).toBe(true);
});

test("an Implementer the hosts do not list is dropped from the query, the boxes and the chips, and a listed one is kept", async () => {
  const daemons = countedFleet();
  await mount("#/runs?implementer=nope,claude%20opus", daemons.fetchImpl);
  for (const base of [DEMO, WRITER]) expect(daemons.lastOf(base).implementer).toBe("claude opus");
  expect((await optionBoxes("Implementer")).map((each) => each.getAttribute("aria-label"))).toEqual(["claude opus", "codex gpt-6-astra"]);
  expect(await checkedIn("Implementer")).toEqual(["claude opus"]);
  expect(chips()).toEqual(["Implementer: claude opus"]);
  expect(filterButton().getAttribute("aria-label")).toBe("Filter and sort, 1 filter on");
});

test("a filter no run matches reads No runs match once with no pager, and an empty scope with no filter names the project and the 7 days", async () => {
  await mount("#/runs?q=nothing", fleet({ rows: runs("DEMO", 100, 3) }, { rows: [] }).fetchImpl);
  expect(screen.getAllByText("No runs match")).toHaveLength(1);
  expect(screen.queryByRole("navigation", { name: "Pages of runs" })).toBeNull();
  cleanup();

  await mount("#/runs?project=demo", fleet({ rows: [] }, { rows: runs("WRITER", 200, 3) }).fetchImpl);
  expect(screen.getByText("No runs in demo in the last 7 days.")).toBeTruthy();
});

for (const [hash, checked, asked] of [
  ["state=all", ["All"], {}],
  ["state=failed", ["Failed"], { state: "failed" }],
  ["state=failed,nope&failure=verify,nope", ["Failed"], { state: "failed", failure: "verify" }],
] as const) {
  test(`an old or partly unknown link, ${hash}, picks only the state it knows and asks the hosts with it`, async () => {
    const daemons = countedFleet();
    await mount(`#/runs?${hash}`, daemons.fetchImpl);
    expect(pressedTabs()).toEqual([...checked]);
    for (const base of [DEMO, WRITER]) {
      const { state, failure } = daemons.lastOf(base);
      expect({ ...(state ? { state } : {}), ...(failure ? { failure } : {}) }).toEqual(asked);
    }
  });
}
