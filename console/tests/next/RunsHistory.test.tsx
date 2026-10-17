import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, renderHook, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { HistoryRow, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { useHistoryDays, useHistoryTotal } from "../../src/next/queries";
import { pastDays } from "../../src/next/runDays";
import { fakeDeps, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const WRITER = "http://writer-host:7711";
const NOW = Date.parse("2026-10-16T12:00:00-07:00");
const MINUTE = 60_000;
const midnight = (date: string) => Date.parse(`${date}T00:00:00-07:00`);
const working = await Bun.file(new URL("../../../tests/fixtures/serve/status.json", import.meta.url)).json() as Status;
const firstPage = ["Wed 14 Oct", "Tue 13 Oct", "Mon 12 Oct", "Sun 11 Oct", "Sat 10 Oct"];

const run = (id: number, outcome: string, work: number, estimate: number | null): HistoryRow => ({
  id, ticket: `DEMO-${id}`, title: `Run ${id}`, ticket_url: null, attempt: 1, phase: "done", outcome,
  outcome_reason: null, failure_kind: null, park_kind: null, started_ms: midnight("2026-10-14") + id * MINUTE,
  ended_ms: midnight("2026-10-14") + (id + 60) * MINUTE, elapsed_ms: 60 * MINUTE,
  heartbeat_age_ms: null, rounds: 0, max_rounds: 3, working_ms: work * MINUTE,
  estimate_ms: estimate === null ? null : estimate * MINUTE, pr_url: null, host: null,
});
const demoRows = [run(1, "merged", 30, 40), run(2, "failed", 20, 20)];
const writerRows = [run(3, "merged", 50, 40), run(4, "abandoned", 16, 20), run(5, "paused", 999, null)];

function fleet(demo: HistoryRow[] | null = demoRows, writer: HistoryRow[] | null = writerRows) {
  const requests: string[] = [];
  const bodies = (project: string) => ({
    status: { ...working, project, runs: [], queued: [] },
    attention: { level: "none", now: NOW, project, items: [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: bodies("/srv/dev/demo"), [WRITER]: bodies("/srv/dev/writer") });
  const fetch: Fetch = async (url) => {
    requests.push(url);
    const parsed = new URL(url);
    if (parsed.pathname !== "/history") return polls(url);
    const rows = parsed.origin === DEMO ? demo : writer;
    if (rows === null) return new Response("not found", { status: 404 });
    const since = Number(parsed.searchParams.get("since"));
    const until = Number(parsed.searchParams.get("until"));
    const limit = Number(parsed.searchParams.get("limit"));
    if (since === midnight("2026-09-15") && until === midnight("2026-10-15") && limit === 1) {
      return Response.json({ rows: [], since, until, limit, truncated: true, total: parsed.origin === DEMO ? 120 : 39, now: NOW });
    }
    const allowed = Array.from({ length: 30 }, (_, i) => midnight("2026-10-14") - i * 86_400_000);
    if (limit !== 500 || !allowed.includes(since) || until !== since + 86_400_000) {
      return new Response("wrong window", { status: 400 });
    }
    const dayRows = since === midnight("2026-10-14") ? rows : [];
    return Response.json({ rows: dayRows, since, until, limit, truncated: false, total: dayRows.length, now: NOW });
  };
  return { fetch, requests };
}

async function settlePage() {
  for (let round = 0; round < 4; round += 1) await act(settle);
}

async function mount(hash: string, fetch: Fetch) {
  window.location.hash = hash;
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(<QueryClientProvider client={consoleQueryClient()}><NextConsole base={DEMO} pollDeps={fake.deps} /></QueryClientProvider>);
  await settlePage();
}

const headings = () => screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent);
const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); window.location.hash = ""; });
afterAll(() => {
  happyDOM.setURL("about:blank");
  if (zone == null) delete process.env.TZ;
  else process.env.TZ = zone;
});

test("history asks each host for the five local days before yesterday and keeps Runs current", async () => {
  const server = fleet();
  await mount("#/runs/history", server.fetch);
  expect(screen.getByRole("heading", { level: 1, name: "Runs history" })).toBeTruthy();
  expect(headings()).toEqual(firstPage);
  for (const base of [DEMO, WRITER]) {
    const requests = server.requests.filter((url) => url.startsWith(`${base}/history`) && url.includes("limit=500"));
    expect(requests).toHaveLength(5);
    for (let day = 10; day <= 14; day += 1) {
      const request = requests.find((url) => new URL(url).searchParams.get("since") === String(midnight(`2026-10-${day}`)));
      expect(request).toBeDefined();
      expect(new URL(request!).searchParams.get("until")).toBe(String(midnight(`2026-10-${day + 1}`)));
    }
  }
  const pager = within(screen.getByRole("navigation", { name: "Pages of days" }));
  expect(pager.getByText("Oct 14 to Oct 10 · page 1 of 6")).toBeTruthy();
  expect((pager.getByRole("button", { name: "Newer days" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByRole("link", { name: "Runs" }).getAttribute("aria-current")).toBe("page");
});

test("paging older days updates the link and newer days drops page one while preserving project", async () => {
  await mount("#/runs/history?project=demo", fleet().fetch);
  await act(async () => void fireEvent.click(screen.getByRole("button", { name: "Older days" })));
  await settlePage();
  expect(new URLSearchParams(window.location.hash.split("?")[1]).get("page")).toBe("2");
  expect(new URLSearchParams(window.location.hash.split("?")[1]).get("project")).toBe("demo");
  expect(headings()).toEqual(["Fri 9 Oct", "Thu 8 Oct", "Wed 7 Oct", "Tue 6 Oct", "Mon 5 Oct"]);
  expect((screen.getByRole("button", { name: "Newer days" }) as HTMLButtonElement).disabled).toBe(false);
  await act(async () => void fireEvent.click(screen.getByRole("button", { name: "Newer days" })));
  await settlePage();
  expect(new URLSearchParams(window.location.hash.split("?")[1]).has("page")).toBe(false);
  expect(headings()).toEqual(firstPage);
});

test.each([
  ["6", "Sep 19 to Sep 15 · page 6 of 6", "Sat 19 Sept", true],
  ["9", "Oct 14 to Oct 10 · page 1 of 6", "Wed 14 Oct", false],
  ["x", "Oct 14 to Oct 10 · page 1 of 6", "Wed 14 Oct", false],
])("history page=%s respects the six-page boundary", async (page, range, first, disabled) => {
  await mount(`#/runs/history?page=${page}`, fleet().fetch);
  expect(screen.getByText(range as string)).toBeTruthy();
  expect(headings()[0]).toBe(first);
  expect((screen.getByRole("button", { name: "Older days" }) as HTMLButtonElement).disabled).toBe(disabled);
});

test("the daylight-saving fall-back day spans its two local midnights and lasts 25 hours", () => {
  const days = pastDays(Date.parse("2026-11-03T12:00:00-08:00"));
  expect(days).toHaveLength(30);
  expect(days[0]).toMatchObject({ since: Date.parse("2026-11-01T00:00:00-07:00"), until: Date.parse("2026-11-02T00:00:00-08:00") });
  expect(days[0]!.until - days[0]!.since).toBe(25 * 60 * MINUTE);
});

test("day heads combine hosts' state counts and sum only runs carrying both work and estimate", async () => {
  await mount("#/runs/history", fleet().fetch);
  const day = screen.getByRole("region", { name: "Wed 14 Oct" });
  for (const text of ["5 runs", "2 merged", "1 failed", "1 abandoned", "1h 56m of work against 2h 00m estimated"]) {
    expect(day.textContent).toContain(text);
  }
  expect(screen.getByText("all projects · 159 runs in the 30 days before yesterday · newest day first")).toBeTruthy();
  const empty = screen.getByRole("region", { name: "Tue 13 Oct" });
  expect(within(empty).getByText("No runs this day.")).toBeTruthy();
  expect(empty.textContent).not.toContain("estimated");
  expect(screen.getByRole("link", { name: "Today and yesterday" }).getAttribute("href")).toEndWith("#/runs");
});

test("project scope limits queries and totals, names empty days and survives the link back to Runs", async () => {
  const server = fleet();
  await mount("#/runs/history?project=demo", server.fetch);
  expect(screen.getByText("demo · 120 runs in the 30 days before yesterday · newest day first")).toBeTruthy();
  expect(screen.getByRole("region", { name: "Wed 14 Oct" }).textContent).toContain("2 runs");
  expect(within(screen.getByRole("region", { name: "Tue 13 Oct" })).getByText("No runs in demo this day.")).toBeTruthy();
  expect(server.requests.some((url) => url.startsWith(`${WRITER}/history`))).toBe(false);
  expect(screen.getByRole("link", { name: "Today and yesterday" }).getAttribute("href")).toEndWith("#/runs?project=demo");
});

test("a daemon without history leaves the other host's days visible and omits an incomplete total", async () => {
  await mount("#/runs/history", fleet(null, writerRows).fetch);
  const day = screen.getByRole("region", { name: "Wed 14 Oct" });
  expect(day.textContent).toContain("3 runs");
  expect(day.textContent).toContain("1 merged");
  expect(day.textContent).toContain("1 abandoned");
  expect(screen.getByText("all projects · the 30 days before yesterday · newest day first")).toBeTruthy();
});

test("history withholds the total until initial host discovery and totals have answered", async () => {
  let release!: () => void;
  const discovery = new Promise<void>((resolve) => { release = resolve; });
  const server = fleet();
  await mount("#/runs/history", async (url) => {
    await discovery;
    return server.fetch(url);
  });
  expect(screen.getByText("all projects · the 30 days before yesterday · newest day first")).toBeTruthy();
  expect(screen.queryByText(/0 runs in the 30 days/)).toBeNull();
  release();
  await settlePage();
  expect(screen.getByText("all projects · 159 runs in the 30 days before yesterday · newest day first")).toBeTruthy();
});

test("history hooks retain each host and day's pending, error and success results", async () => {
  const hosts = [DEMO, WRITER].map((base) => ({
    key: base, address: base, base, label: base, project: working.project,
    status: working, attention: null, polled_ms: NOW, seen_ms: NOW, error: null, needs_token: false,
  }));
  const days = pastDays(NOW).slice(0, 2);
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  const failure = new Error("history unavailable");
  const server = fleet();
  const fetch: Fetch = async (url) => {
    await pending;
    if (new URL(url).origin === DEMO) throw failure;
    return server.fetch(url);
  };
  const client = consoleQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const hook = renderHook(() => ({
    days: useHistoryDays(hosts, days, { fetch }),
    total: useHistoryTotal(hosts, NOW, { fetch }),
  }), { wrapper });
  try {
    expect(hook.result.current.days.queries).toHaveLength(4);
    expect(hook.result.current.total.queries).toHaveLength(2);
    for (const query of [...hook.result.current.days.queries, ...hook.result.current.total.queries]) {
      expect(query.status).toBe("pending");
      expect(query.error).toBeNull();
    }
    release();
    await settlePage();
    for (const [index, query] of hook.result.current.days.queries.entries()) {
      expect(query.host).toBe(hosts[Math.floor(index / days.length)]!);
      expect(query.day).toBe(days[index % days.length]!);
      expect(query.status).toBe(query.host.base === DEMO ? "error" : "success");
      expect(query.error).toBe(query.host.base === DEMO ? failure : null);
    }
    for (const [index, query] of hook.result.current.total.queries.entries()) {
      expect(query.host).toBe(hosts[index]!);
      expect(query.status).toBe(query.host.base === DEMO ? "error" : "success");
      expect(query.error).toBe(query.host.base === DEMO ? failure : null);
    }
    expect(hook.result.current.days.answers.get(days[0]!.since)?.get(hosts[1]!.key)?.rows).toHaveLength(3);
    expect(hook.result.current.total.total).toBeUndefined();
  } finally {
    release();
    hook.unmount();
    client.clear();
  }
});
