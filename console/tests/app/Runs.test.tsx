import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createHashHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import type { Attention, HistoryRow, Status } from "../../src/lib/contract.gen";
import type { HostRecord } from "../../src/lib/hosts";
import type { Fetch } from "../../src/lib/poll";
import type { PeersState } from "../../src/hooks/usePeers";
import { consoleQueryClient } from "../../src/app/fleet";
import { RunTable } from "../../src/app/components/RunTable";
import type { ContractBodies } from "../../src/app/fleet";
import type { RunRow } from "../../src/app/history";
import { ConsoleApp } from "../../src/app/router";
import { PeersContext } from "../../src/app/scope";
import ready from "../../../tests/fixtures/serve/run-merge-ready.json";
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

const demo12 = run({ id: 12, ticket: "DEMO-12", title: "Count the rounds", started_ms: today(16, 36) });
const demo9 = run({
  id: 9, ticket: "DEMO-9", title: "Page the ledger", started_ms: new Date(2026, 9, 14, 21, 5).getTime(),
  phase: "done", outcome: "merged", rounds: 2, pr_url: "https://github.com/example/demo/pull/524", elapsed_ms: 103 * MINUTE,
});
const writer40 = run({
  id: 40, ticket: "WRITER-40", title: "Write the notes", started_ms: today(16, 20),
  phase: "failed", outcome: "failed", rounds: 1, elapsed_ms: 9 * MINUTE,
});

type Item = Attention["items"][number];

/** The daemon's state groups: an open run, or a paused one no later run resumed, is active;
 *  a paused run a later run resumed is in none. */
function groupOf(row: HistoryRow): "active" | "merged" | "failed" | "abandoned" | null {
  if (row.outcome == null || (row.outcome === "paused" && row.resumed_as == null)) return "active";
  if (row.outcome === "paused") return null;
  return row.outcome === "merged" || row.outcome === "abandoned" ? row.outcome : "failed";
}

/** Each daemon answers `/history` only at local midnight six days before today's, in the asked `state`,
 *  newest first and cut at `offset` and `limit`; null answers 404.
 *  The demo daemon sends `items` on `/attention`, and only `readyRuns` are ready to merge. */
function fleet(demo: HistoryRow[] | null, writer: HistoryRow[] | null, items: Item[] = [], readyRuns: number[] = []): Fetch {
  const bodies = (project: string, list: Item[]) => ({
    status: { ...working, project, runs: [], queued: [] },
    attention: { level: list.length ? "attention" : "none", now: NOW, project, items: list },
  });
  const polls = peersFetch(DEMO, { [DEMO]: bodies("/srv/dev/demo", items), [WRITER]: bodies("/srv/dev/writer", []) });
  return async (url) => {
    const merge = url.match(/\/runs\/(\d+)\/merge$/);
    if (merge) return Response.json({ ...ready, run: Number(merge[1]), ready: readyRuns.includes(Number(merge[1])) });
    for (const [base, rows] of [[DEMO, demo], [WRITER, writer]] as const) {
      const asked = new URL(url).searchParams;
      if (url.startsWith(`${base}/history?`) && asked.get("since") === String(SINCE) && rows) {
        const state = asked.get("state");
        const matching = rows.filter((row) => state == null || groupOf(row) === state).sort((a, b) => b.started_ms - a.started_ms);
        const offset = Number(asked.get("offset") ?? 0);
        const limit = Number(asked.get("limit") ?? 200);
        const counts = { active: 0, merged: 0, failed: 0, abandoned: 0 };
        for (const row of rows) {
          const group = groupOf(row);
          if (group) counts[group] += 1;
        }
        return Response.json({
          rows: matching.slice(offset, offset + limit), since: SINCE, until: null, limit, offset, truncated: matching.length > offset + limit,
          total: matching.length, counts: { state: counts, failure: {}, implementer: {} }, now: NOW,
        });
      }
    }
    return polls(url);
  };
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

const rows = () => within(screen.getByRole("list", { name: "Runs, newest first" })).queryAllByRole("listitem");
const runLink = (row: HTMLElement) => within(row).getByRole("link", { name: /^Run \d+,/ });
const cells = (row: HTMLElement) => Array.from(row.children, (cell) => cell.textContent);
const stateCells = () => rows().map((row) => cells(row)[4]);

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

test("Runs lists every host's runs since yesterday newest start first, each named and linked to its run", async () => {
  await mount("#/runs?state=all", fleet([demo12, demo9], [writer40]));
  expect(rows().map((row) => row.getAttribute("data-run"))).toEqual(["12", "40", "9"]);
  const [first, failed, merged] = rows();
  expect(runLink(first!).getAttribute("aria-label")).toBe("Run 12, DEMO-12 Count the rounds, demo, reviewing · round 1 of 3, started 16:36");
  expect(runLink(first!).getAttribute("href")).toEndWith("#/runs/demo/12");

  expect(within(failed!).getByRole("img").getAttribute("aria-label")).toBe("failed");
  expect(cells(merged!)).toContain("yest 21:05");
  expect(runLink(merged!).getAttribute("href")).toEndWith("#/runs/demo/9");
  expect(within(merged!).getByRole("img").getAttribute("aria-label")).toBe("merged");
});

test("a Runs row opens its run's Run page on its own project", async () => {
  const writer7 = run({ id: 7, ticket: "WRITER-7", title: "Name the writer", started_ms: today(17, 0) });
  await mount("#/runs", fleet([demo12], [writer7]));
  const row = rows().find((each) => each.getAttribute("data-run") === "7")!;
  expect(runLink(row).getAttribute("href")).toEndWith("#/runs/writer/7");
  await act(async () => void fireEvent.click(runLink(row)));
  await act(settle);
  expect(window.location.hash).toBe("#/runs/writer/7");
});

test("a Runs row whose host has not answered /status reads its run as text, with no link", async () => {
  const silent: HostRecord<ContractBodies> = {
    key: "writer-host:7711", address: "writer-host:7711", base: WRITER, label: "writer-host:7711", project: null,
    status: null, attention: null, polled_ms: NOW, seen_ms: null, error: "connection refused", needs_token: false,
  };
  const row: RunRow = {
    ...run({ id: 7, ticket: "WRITER-7", title: "Name the writer", started_ms: today(17, 0) }),
    base: WRITER, project: "writer", state: "running", seen_ms: null, thresholds: working.thresholds, stage: "reviewing",
  };
  const peers: PeersState<ContractBodies> = { hosts: [silent], polledAgo: 0, polls: 1, now: NOW };
  const root = createRootRoute({
    component: () => (
      <PeersContext.Provider value={peers}>
        <RunTable rows={[row]} now={NOW} empty="" />
      </PeersContext.Provider>
    ),
  });
  const page = createRoute({ getParentRoute: () => root, path: "/runs/$project/$run" });
  window.location.hash = "#/";
  render(<RouterProvider router={createRouter({ routeTree: root.addChildren([page]), history: createHashHistory() })} />);
  await act(settle);
  const [row7] = rows();
  const runCell = row7!.children[0]!;
  expect(runCell.textContent).toBe("7");
  expect(runCell.closest("a")).toBeNull();
  expect(runCell.querySelector("a")).toBeNull();
  expect(within(row7!).queryByRole("link", { name: /^Run 7,/ })).toBeNull();
  expect(Array.from(row7!.querySelectorAll("[href]"), (link) => link.getAttribute("href")).filter((href) => href!.includes("classic"))).toEqual([]);
});

test("a scoped Runs lists only that project's runs, with a dash for no rounds or pull request", async () => {
  await mount("#/runs?project=demo&state=all", fleet([demo12, demo9], [writer40]));
  const [first, second] = rows();
  expect(rows()).toHaveLength(2);
  expect(cells(first!)).toEqual(["12", "DEMO-12", "Count the rounds", "demo", "reviewing · round 1 of 3", "–", "–", "16:36", "–", "16m"]);
  expect(cells(second!)).toEqual(["9", "DEMO-9", "Page the ledger", "demo", "merged", "2", "#524", "yest 21:05", "–", "1h 43m"]);
});

test("the table heads the ticket key and the title as separate columns, the key linked to its ticket page", async () => {
  await mount("#/runs", fleet([run({ id: 21, ticket: "HOLO-12", title: "Fix the sweep", started_ms: today(17, 0) })], []));
  const head = document.querySelector("[data-run-heads]")!;
  expect(Array.from(head.children, (cell) => cell.textContent)).toEqual(["Run", "Ticket", "Title", "Project", "State", "Rounds", "PR", "Started", "Ended", "Took"]);
  const [row] = rows();
  expect(cells(row!).slice(1, 3)).toEqual(["HOLO-12", "Fix the sweep"]);
  expect(runLink(row!).getAttribute("aria-label")).toStartWith("Run 21, HOLO-12 Fix the sweep, demo,");
  expect(within(row!).getByRole("link", { name: "HOLO-12" }).getAttribute("href")).toEndWith("#/tickets/demo/HOLO-12");
});

test("a host whose daemon has no /history adds no rows and no error beside one that answers", async () => {
  await mount("#/runs?state=all", fleet(null, [writer40]));
  expect(rows().map((row) => row.getAttribute("data-run"))).toEqual(["40"]);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(document.querySelector("[data-notice]")).toBeNull();
});

const pullRequestItem = (id: number, pr: object): Item => ({
  kind: "pr_open", level: "attention", run: id, ticket: `DEMO-${id}`, ticket_url: null, title: `Ticket ${id}`,
  park_kind: "pull_request", reason: "PR open", asked_ms: NOW - MINUTE, pr_url: `https://github.com/example/demo/pull/${id}`,
  pr: { number: id, checks: "success", review: "approved", threads: 0, open_threads: 0, title: null, state: "ready", ...pr },
} as Item);
const parkedRun = (id: number, hour: number) => run({
  id, ticket: `DEMO-${id}`, title: `Park ${id}`, started_ms: today(hour, 0), phase: "awaiting_merge_approval", park_kind: "pull_request",
  pr_url: `https://github.com/example/demo/pull/${id}`,
});

test("a failed run's State cell names its failure stage and an abandoned run's its reason's first line", async () => {
  const failed = run({ id: 11, ticket: "DEMO-11", title: "Fail", started_ms: today(16, 0), phase: "failed", outcome: "failed", failure_kind: "verify" });
  const abandoned = run({
    id: 10, ticket: "DEMO-10", title: "Drop", started_ms: today(15, 0), phase: "done", outcome: "abandoned",
    outcome_reason: "aborted: wrong ticket\nmore",
  });
  await mount("#/runs?state=all", fleet([failed, abandoned], []));
  expect(stateCells()).toEqual(["failed at verify", "abandoned · aborted: wrong ticket"]);
});

test("a parked run names its pull request's first open fact and a stale run how long since its beat", async () => {
  const parked = parkedRun(8, 17);
  const stale = run({ id: 7, ticket: "DEMO-7", title: "Hang", started_ms: today(16, 50), phase: "implementing", heartbeat_age_ms: 15 * MINUTE });
  await mount("#/runs", fleet([parked, stale], [], [pullRequestItem(8, { threads: 1, open_threads: 1 })]));
  expect(stateCells()).toEqual(["parked · 1 thread open", "stale · no beat 15m"]);
});

test("exactly the runs that wait on the maintainer carry data-need, each row named by its stage word", async () => {
  const stale = run({ id: 7, ticket: "DEMO-7", title: "Hang", started_ms: today(17, 50), heartbeat_age_ms: 15 * MINUTE });
  const question = run({ id: 6, ticket: "DEMO-6", title: "Ask", started_ms: today(17, 40), phase: "blocked_on_operator" });
  const paused = run({ id: 5, ticket: "DEMO-5", title: "Hold", started_ms: today(17, 30), phase: "paused", outcome: "paused" });
  const readyRun = parkedRun(4, 17);
  const merged = run({ id: 3, ticket: "DEMO-3", title: "Ship", started_ms: today(10, 0), phase: "done", outcome: "merged", ended_ms: today(11, 15) });
  const items = [
    { kind: "blocked", level: "attention", run: 6, ticket: "DEMO-6", ticket_url: null, question: "Which way?\nLeft or right", asked_ms: NOW - MINUTE, pr_url: null, park_kind: null, pr: null },
    { kind: "paused", level: "attention", run: 5, ticket: "DEMO-5", ticket_url: null, title: "Hold", note: "Waiting on a key", asked_ms: NOW - MINUTE, pr_url: null },
    pullRequestItem(4, {}),
  ] as Item[];
  await mount("#/runs?state=all", fleet([stale, question, paused, readyRun, { ...demo12, rounds: 1 }, merged], [], items, [4]));
  expect(stateCells()).toEqual([
    "stale · no beat 15m", "asks · Which way?", "paused · Waiting on a key", "ready to merge", "reviewing · round 2 of 3", "merged 11:15",
  ]);
  expect(rows().map((row) => row.hasAttribute("data-need"))).toEqual([true, true, true, true, false, false]);
  expect(runLink(rows()[4]!).getAttribute("aria-label")).toEndWith(", reviewing · round 2 of 3, started 16:36");
});

test("the Ended column, between Started and Took, reads an ended run's end as Started reads a start and a dash while a run is open", async () => {
  const endedYesterday = run({
    id: 30, ticket: "DEMO-30", title: "End late", started_ms: new Date(2026, 9, 14, 15, 50).getTime(),
    phase: "done", outcome: "merged", ended_ms: new Date(2026, 9, 14, 16, 20).getTime(),
  });
  await mount("#/runs?state=all", fleet([demo12, endedYesterday], []));
  const [open, closed] = rows();
  expect(cells(open!)[8]).toBe("–");
  expect(cells(closed!).slice(7)).toEqual(["yest 15:50", "yest 16:20", "16m"]);
});

const ended = (outcome: string) => ({ phase: "done", outcome, ended_ms: today(17, 0) });
const demoParked = run({ id: 13, ticket: "DEMO-13", title: "Park the pull", started_ms: today(15, 0), phase: "awaiting_merge_approval" });
const demoFailed = run({ id: 14, ticket: "DEMO-14", title: "Fail the build", started_ms: today(14, 0), ...ended("failed") });
const writer41 = run({ id: 41, ticket: "WRITER-41", title: "Draft the notes", started_ms: today(13, 0) });
const writerMerged = run({ id: 42, ticket: "WRITER-42", title: "Merge the notes", started_ms: today(12, 0), ...ended("merged") });
const writerAbandoned = run({ id: 43, ticket: "WRITER-43", title: "Drop the notes", started_ms: today(11, 0), ...ended("abandoned") });
const sixRuns = () => fleet([demo12, demoParked, demoFailed], [writer41, writerMerged, writerAbandoned]);

const countLine = () => document.querySelector("[data-count-line]")!.textContent;
const openFilter = async () => {
  if (!screen.queryByRole("dialog", { name: "Filter runs" })) await act(async () => void fireEvent.click(screen.getByRole("button", { name: /^Filter/ })));
  return within(screen.getByRole("group", { name: "State" }));
};
const stateRows = async () => (await openFilter()).getAllByRole("checkbox").slice(1).map((box) => box.closest("label")!.textContent);
const checkedStates = async () => (await openFilter()).getAllByRole("checkbox").slice(1).filter((box) => (box as HTMLInputElement).checked).map((box) => box.getAttribute("aria-label"));
const checkState = async (name: string) => {
  const box = (await openFilter()).getByRole("checkbox", { name });
  await act(async () => {
    fireEvent.click(box);
    await settle();
  });
  for (let round = 0; round < 3; round += 1) await act(settle);
};
const searchState = () => new URLSearchParams(window.location.hash.split("?")[1]).get("state");
const shownIds = () => rows().map((row) => row.getAttribute("data-run"));

function resumedFleet(): Fetch {
  const resumed = run({ id: 5, ticket: "DEMO-5", title: "Hold", started_ms: today(17, 30), phase: "paused", outcome: "paused", resumed_as: 8 });
  const resumer = run({ id: 8, ticket: "DEMO-5", title: "Hold", started_ms: today(17, 50) });
  const waiting = run({ id: 4, ticket: "DEMO-4", title: "Wait", started_ms: today(17, 0), phase: "paused", outcome: "paused", resumed_as: null });
  const items = [
    { kind: "paused", level: "attention", run: 4, ticket: "DEMO-4", ticket_url: null, title: "Wait", note: "Waiting on a key", asked_ms: NOW - MINUTE, pr_url: null },
  ] as Item[];
  return fleet([resumed, resumer, waiting], [], items);
}

test("a paused run a later run resumed reads resumed as that run in grey, while one with no later run still needs you", async () => {
  await mount("#/runs?state=all", resumedFleet());
  const byId = (id: string) => rows().find((row) => row.getAttribute("data-run") === id)!;
  const [resumed, waiting] = [byId("5"), byId("4")];
  expect(cells(resumed)[4]).toBe("paused · resumed as run 8");
  expect(within(resumed).getByRole("link", { name: "run 8" }).getAttribute("href")).toEndWith("#/runs/demo/8");
  expect(resumed.hasAttribute("data-need")).toBe(false);
  expect(within(resumed).getByText(/^paused · /).className).toContain("text-ink-3");
  expect(cells(waiting)[4]).toBe("paused · Waiting on a key");
  expect(waiting.hasAttribute("data-need")).toBe(true);
});

test("Active under State leaves out a paused run a later run resumed and does not count it, while no state lists it", async () => {
  await mount("#/runs", resumedFleet());
  expect(await checkedStates()).toEqual([]);
  expect(shownIds()).toEqual(["8", "5", "4"]);
  expect(countLine()).toBe("all projects · 3 runs / 2 active / newest first");
  await checkState("Active");
  expect(await checkedStates()).toEqual(["Active"]);
  expect(shownIds()).toEqual(["8", "4"]);
  expect(countLine()).toBe("all projects · 2 runs / 2 active / newest first");
});

test("the count line and every State box count the scoped runs, with no State box checked and every run listed by default", async () => {
  await mount("#/runs", sixRuns());
  expect(countLine()).toBe("all projects · 6 runs / 3 active / newest first");
  expect(await stateRows()).toEqual(["Active 3", "Merged 1", "Failed 1", "Abandoned 1"]);
  expect(await checkedStates()).toEqual([]);
  expect(shownIds()).toEqual(["12", "13", "14", "41", "42", "43"]);
});

test("checking Active under State from the default puts state=active in the link and lists only the active runs", async () => {
  await mount("#/runs", sixRuns());
  await checkState("Active");
  expect(searchState()).toBe("active");
  expect(await checkedStates()).toEqual(["Active"]);
  expect(shownIds()).toEqual(["12", "13", "41"]);
});

test("a link with state=merged checks Merged under State and lists only the merged run", async () => {
  await mount("#/runs?state=merged", sixRuns());
  expect(await checkedStates()).toEqual(["Merged"]);
  expect(shownIds()).toEqual(["42"]);
});

test("checking Failed under State puts state=failed in the link and lists only the failed run", async () => {
  await mount("#/runs", sixRuns());
  await checkState("Failed");
  expect(new URLSearchParams(window.location.hash.split("?")[1]).get("state")).toBe("failed");
  expect(await checkedStates()).toEqual(["Failed"]);
  expect(rows().map((row) => row.getAttribute("data-run"))).toEqual(["14"]);
});

test("a state the filter does not know reads as no state", async () => {
  await mount("#/runs?state=bogus", sixRuns());
  expect(await checkedStates()).toEqual([]);
  expect(shownIds()).toEqual(["12", "13", "14", "41", "42", "43"]);
});

test("a scoped filter with no runs says no runs match, with no Show all projects line", async () => {
  sessionStorage.setItem("holophyte.next.project", "demo");
  await mount("#/runs?project=demo&state=merged", sixRuns());
  expect(rows()).toHaveLength(0);
  expect(screen.getByText("No runs match")).toBeTruthy();
  expect(screen.queryByText(/^Showing/)).toBeNull();
  expect(screen.queryByRole("button", { name: "Show all projects" })).toBeNull();
  expect(sessionStorage.getItem("holophyte.next.project")).toBe("demo");
});

test("the footer links to older days with project scope and names the time zone", async () => {
  await mount("#/runs?project=demo", sixRuns());
  const footer = document.querySelector("section footer")!;
  expect(within(footer as HTMLElement).getByRole("link", { name: "Older days" }).getAttribute("href")).toEndWith("#/runs/history?project=demo");
  expect(Array.from(footer.children, (line) => line.textContent)).toEqual([
    "Active runs also show on the Overview. Open a run for its stage, log and agent.",
    "Older days",
    "times show in PDT",
  ]);
});
