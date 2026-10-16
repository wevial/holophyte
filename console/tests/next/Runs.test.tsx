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
import { consoleQueryClient } from "../../src/next/fleet";
import { RunTable } from "../../src/next/components/RunTable";
import type { ContractBodies } from "../../src/next/fleet";
import type { RunRow } from "../../src/next/history";
import { NextConsole } from "../../src/next/router";
import { PeersContext } from "../../src/next/scope";
import ready from "../../../tests/fixtures/serve/run-merge-ready.json";
import { fakeDeps, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const WRITER = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;

const MINUTE = 60_000;
const today = (hour: number, minute: number) => new Date(2026, 9, 15, hour, minute).getTime();
const NOW = today(18, 0);
const YESTERDAY = new Date(2026, 9, 14, 0, 0).getTime();

const run = (row: Partial<HistoryRow> & Pick<HistoryRow, "id" | "ticket" | "title" | "started_ms">): HistoryRow => ({
  ticket_url: null, attempt: 1, phase: "reviewing", outcome: null, outcome_reason: null, failure_kind: null,
  park_kind: null, ended_ms: null, elapsed_ms: 16 * MINUTE, heartbeat_age_ms: 1_000, rounds: 0, max_rounds: 3,
  pr_url: null, host: null, ...row,
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

/** Each daemon answers `/history` only at yesterday's local midnight; null answers 404.
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
      if (url === `${base}/history?since=${YESTERDAY}` && rows) {
        return Response.json({ rows, since: YESTERDAY, limit: 500, truncated: false, now: NOW });
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
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const rows = () => within(screen.getByRole("list", { name: "Runs, newest first" })).queryAllByRole("link");
const cells = (row: HTMLElement) => Array.from(row.children, (cell) => cell.textContent);
const stateCells = () => rows().map((row) => cells(row)[3]);

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
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
  await mount("#/runs", fleet([demo12, demo9], [writer40]));
  expect(rows().map((row) => row.getAttribute("data-run"))).toEqual(["12", "40", "9"]);
  const [first, failed, merged] = rows();
  expect(first!.getAttribute("aria-label")).toBe("Run 12, DEMO-12 Count the rounds, demo, reviewing · round 1 of 3, started 16:36");
  expect(first!.getAttribute("href")).toEndWith("#/runs/demo/12");
  expect(within(failed!).getByRole("img").getAttribute("aria-label")).toBe("failed");
  expect(cells(merged!)).toContain("yest 21:05");
  expect(within(merged!).getByRole("img").getAttribute("aria-label")).toBe("merged");
});

test("a Runs row opens its run's Run page on its own project", async () => {
  const writer7 = run({ id: 7, ticket: "WRITER-7", title: "Name the writer", started_ms: today(17, 0) });
  await mount("#/runs", fleet([demo12], [writer7]));
  const row = rows().find((each) => each.getAttribute("data-run") === "7")!;
  expect(row.getAttribute("href")).toEndWith("#/runs/writer/7");
  await act(async () => void fireEvent.click(row));
  await act(settle);
  expect(window.location.hash).toBe("#/runs/writer/7");
});

test("a Runs row whose host has not answered /status keeps the classic run link", async () => {
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
  expect(rows()[0]!.getAttribute("href")).toBe(`/?classic#run=7&daemon=${encodeURIComponent(WRITER)}`);
});

test("a scoped Runs lists only that project's runs, with a dash for no rounds or pull request", async () => {
  await mount("#/runs?project=demo", fleet([demo12, demo9], [writer40]));
  const [first, second] = rows();
  expect(rows()).toHaveLength(2);
  expect(cells(first!)).toEqual(["12", "DEMO-12 Count the rounds", "demo", "reviewing · round 1 of 3", "–", "–", "16:36", "16m"]);
  expect(cells(second!)).toEqual(["9", "DEMO-9 Page the ledger", "demo", "merged", "2", "#524", "yest 21:05", "1h 43m"]);
});

test("a host whose daemon has no /history adds no rows and no error beside one that answers", async () => {
  await mount("#/runs", fleet(null, [writer40]));
  expect(rows().map((row) => row.getAttribute("data-run"))).toEqual(["40"]);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(document.querySelector("[data-notice]")).toBeNull();
});

const pullRequestItem = (id: number, pr: object): Item => ({
  kind: "pr_open", level: "attention", run: id, ticket: `DEMO-${id}`, ticket_url: null, title: `Ticket ${id}`,
  park_kind: "pull_request", reason: "PR open", asked_ms: NOW - MINUTE, pr_url: `https://github.com/example/demo/pull/${id}`,
  pr: { state: null, number: id, checks: "success", review: "approved", threads: 0, open_threads: 0, title: null, ...pr },
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
  await mount("#/runs", fleet([failed, abandoned], []));
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
  await mount("#/runs", fleet([stale, question, paused, readyRun, { ...demo12, rounds: 1 }, merged], [], items, [4]));
  expect(stateCells()).toEqual([
    "stale · no beat 15m", "asks · Which way?", "paused · Waiting on a key", "ready to merge", "reviewing · round 2 of 3", "merged 11:15",
  ]);
  expect(rows().map((row) => row.hasAttribute("data-need"))).toEqual([true, true, true, true, false, false]);
  expect(rows()[4]!.getAttribute("aria-label")).toEndWith(", reviewing · round 2 of 3, started 16:36");
});

const ended = (outcome: string) => ({ phase: "done", outcome, ended_ms: today(17, 0) });
const demoParked = run({ id: 13, ticket: "DEMO-13", title: "Park the pull", started_ms: today(15, 0), phase: "awaiting_merge_approval" });
const demoFailed = run({ id: 14, ticket: "DEMO-14", title: "Fail the build", started_ms: today(14, 0), ...ended("failed") });
const writer41 = run({ id: 41, ticket: "WRITER-41", title: "Draft the notes", started_ms: today(13, 0) });
const writerMerged = run({ id: 42, ticket: "WRITER-42", title: "Merge the notes", started_ms: today(12, 0), ...ended("merged") });
const writerAbandoned = run({ id: 43, ticket: "WRITER-43", title: "Drop the notes", started_ms: today(11, 0), ...ended("abandoned") });
const sixRuns = () => fleet([demo12, demoParked, demoFailed], [writer41, writerMerged, writerAbandoned]);

const countLine = () => document.querySelector("[data-count-line]")!.textContent;
const filterButtons = () => within(screen.getByRole("group", { name: "State" })).getAllByRole("button");
const pressed = () => filterButtons().filter((button) => button.getAttribute("aria-pressed") === "true").map((button) => button.textContent);

test("the count line and every State button count the scoped runs, with All pressed by default", async () => {
  await mount("#/runs", sixRuns());
  expect(countLine()).toBe("all projects · 6 runs / 3 active / newest first / today and yesterday");
  expect(filterButtons().map((button) => button.textContent)).toEqual(["All 6", "Active 3", "Merged 1", "Failed 1", "Abandoned 1"]);
  expect(pressed()).toEqual(["All 6"]);
});

test("pressing Failed puts state=failed in the link and lists only the failed run", async () => {
  await mount("#/runs", sixRuns());
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Failed 1" }));
    await settle();
  });
  expect(new URLSearchParams(window.location.hash.split("?")[1]).get("state")).toBe("failed");
  expect(pressed()).toEqual(["Failed 1"]);
  expect(rows().map((row) => row.getAttribute("data-run"))).toEqual(["14"]);
});

test("a state the filter does not know reads as All and lists every run", async () => {
  await mount("#/runs?state=bogus", sixRuns());
  expect(pressed()).toEqual(["All 6"]);
  expect(rows()).toHaveLength(6);
});

test("a scoped filter with no runs says so, and Show all projects drops the project but keeps the state", async () => {
  sessionStorage.setItem("holophyte.next.project", "demo");
  await mount("#/runs?project=demo&state=merged", sixRuns());
  expect(screen.getByText(/^Showing/).textContent).toBe("Showing demo only");
  expect(rows()).toHaveLength(0);
  expect(screen.getByText("No merged runs in demo today or yesterday.")).toBeTruthy();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Show all projects" }));
    await settle();
  });
  const search = new URLSearchParams(window.location.hash.split("?")[1]);
  expect(search.get("state")).toBe("merged");
  expect(search.has("project")).toBe(false);
  expect(sessionStorage.getItem("holophyte.next.project")).toBeNull();
});

test("the footer points to the Overview and names the time zone the times are in", async () => {
  await mount("#/runs", sixRuns());
  const footer = document.querySelector("section footer")!;
  expect(Array.from(footer.children, (line) => line.textContent)).toEqual([
    "Active runs also show on the Overview. Open a run for its stage, log and agent.",
    "times PDT",
  ]);
});
