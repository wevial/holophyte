import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { HistoryRow, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
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

/** Each daemon answers `/history` only at yesterday's local midnight; null answers 404. */
function fleet(demo: HistoryRow[] | null, writer: HistoryRow[] | null): Fetch {
  const bodies = (project: string) => ({
    status: { ...working, project, runs: [], queued: [] },
    attention: { level: "none", now: NOW, project, items: [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: bodies("/srv/dev/demo"), [WRITER]: bodies("/srv/dev/writer") });
  return async (url) => {
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
  expect(first!.getAttribute("aria-label")).toBe("Run 12, DEMO-12 Count the rounds, demo, running, started 16:36");
  expect(first!.getAttribute("href")).toBe(`/?classic#run=12&daemon=${encodeURIComponent(DEMO)}`);
  expect(within(failed!).getByRole("img").getAttribute("aria-label")).toBe("failed");
  expect(cells(merged!)).toContain("yest 21:05");
  expect(within(merged!).getByRole("img").getAttribute("aria-label")).toBe("merged");
});

test("a scoped Runs lists only that project's runs, with a dash for no rounds or pull request", async () => {
  await mount("#/runs?project=demo", fleet([demo12, demo9], [writer40]));
  const [first, second] = rows();
  expect(rows()).toHaveLength(2);
  expect(cells(first!)).toEqual(["12", "DEMO-12 Count the rounds", "demo", "running", "–", "–", "16:36", "16m"]);
  expect(cells(second!)).toEqual(["9", "DEMO-9 Page the ledger", "demo", "merged", "2", "#524", "yest 21:05", "1h 43m"]);
});

test("a host whose daemon has no /history adds no rows and no error beside one that answers", async () => {
  await mount("#/runs", fleet(null, [writer40]));
  expect(rows().map((row) => row.getAttribute("data-run"))).toEqual(["40"]);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(document.querySelector("[data-notice]")).toBeNull();
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
