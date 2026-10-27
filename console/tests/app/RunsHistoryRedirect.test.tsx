import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { HistoryRow, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const WRITER = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;

const NOW = new Date(2026, 9, 15, 18, 0).getTime();
const THIRTY_DAYS_SINCE = String(new Date(2026, 8, 16).getTime());

const failed: HistoryRow = {
  id: 7, ticket: "DEMO-7", title: "Fail the verify", ticket_url: null, attempt: 1, started_ms: NOW - 3 * 86_400_000,
  phase: "failed", outcome: "failed", outcome_reason: null, failure_kind: "verify", park_kind: null, ended_ms: null,
  elapsed_ms: 60_000, heartbeat_age_ms: null, rounds: 0, max_rounds: 3, working_ms: null, estimate_ms: null, pr_url: null, host: null,
};

/** Each daemon answers any `/history` with `rows`; every query's parameters land in `asked`. */
function fleet(rows: HistoryRow[]) {
  const asked: Record<string, string>[] = [];
  const bodies = (project: string) => ({
    status: { ...working, project, runs: [], queued: [] },
    attention: { level: "none", now: NOW, project, items: [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: bodies("/srv/dev/demo"), [WRITER]: bodies("/srv/dev/writer") });
  const fetchImpl: Fetch = async (url) => {
    if (!url.includes("/history?")) return polls(url);
    const params = new URL(url).searchParams;
    asked.push(Object.fromEntries(params));
    const limit = Number(params.get("limit") ?? 200);
    return Response.json({
      rows, since: Number(params.get("since")), until: null, limit, offset: 0, truncated: false, total: rows.length,
      counts: { state: { active: 0, merged: 0, failed: rows.length, abandoned: 0 }, failure: {}, implementer: {} }, implementers: [], now: NOW,
    });
  };
  return { fetchImpl, asked };
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

const hashPath = () => window.location.hash.split("?")[0];
const hashSearch = () => Object.fromEntries(new URLSearchParams(window.location.hash.split("?")[1]));

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

test("an old history link opens Runs over the last 30 days, keeping its project and reading its outcome as the state", async () => {
  const daemon = fleet([failed]);
  await mount("#/runs/history?project=demo&outcome=failed&page=3", daemon.fetchImpl);
  expect(hashPath()).toBe("#/runs");
  expect(hashSearch()).toEqual({ project: "demo", state: "failed", range: "30d" });
  expect(screen.getByRole("list", { name: "Runs, newest first" })).toBeTruthy();
  expect(daemon.asked.at(-1)).toMatchObject({ since: THIRTY_DAYS_SINCE, state: "failed" });
});

test("a bare history link opens Runs over the last 30 days", async () => {
  await mount("#/runs/history", fleet([]).fetchImpl);
  expect(hashPath()).toBe("#/runs");
  expect(hashSearch()).toEqual({ range: "30d" });
});

test("an outcome the history page did not know opens Runs in no state", async () => {
  await mount("#/runs/history?outcome=nope", fleet([]).fetchImpl);
  expect(hashPath()).toBe("#/runs");
  expect(hashSearch()).toEqual({ range: "30d" });
});
