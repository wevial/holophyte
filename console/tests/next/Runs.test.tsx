import { afterAll, afterEach, beforeEach, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, render, screen, within } from "@testing-library/react";
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

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  window.location.hash = "";
});
afterAll(() => {
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
