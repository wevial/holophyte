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
    const dayRows = rows.filter((row) => row.started_ms! >= since && row.started_ms! < until);
    return Response.json({ rows: dayRows, since, until, limit, truncated: false, total: dayRows.length, now: NOW });
  };
  return { fetch, requests };
}

async function settlePage() {
  for (let round = 0; round < 4; round += 1) await act(settle);
}

async function mount(hash: string, fetch: Fetch, now = NOW) {
  window.location.hash = hash;
  const fake = fakeDeps(fetch);
  fake.clock.now = now;
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
  expect(screen.getByText("five days a page · 30 days back")).toBeTruthy();
  expect(screen.getByText("times PDT")).toBeTruthy();

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
  expect(screen.queryAllByRole("heading", { level: 2 })).toHaveLength(0);
  expect(screen.getByText("No runs in demo from Oct 9 to Oct 5. Try older days.")).toBeTruthy();
  expect((screen.getByRole("button", { name: "Newer days" }) as HTMLButtonElement).disabled).toBe(false);
  await act(async () => void fireEvent.click(screen.getByRole("button", { name: "Newer days" })));
  await settlePage();
  expect(new URLSearchParams(window.location.hash.split("?")[1]).has("page")).toBe(false);
  expect(headings()).toEqual(firstPage);
});

test.each([
  ["6", "Sep 19 to Sep 15 · page 6 of 6", undefined, true],
  ["9", "Oct 14 to Oct 10 · page 1 of 6", "Wed 14 Oct", false],
  ["x", "Oct 14 to Oct 10 · page 1 of 6", "Wed 14 Oct", false],
])("history page=%s respects the six-page boundary", async (page, range, first, disabled) => {
  await mount(`#/runs/history?page=${page}`, fleet().fetch);
  expect(screen.getByText(range as string)).toBeTruthy();
  if (first === undefined) expect(screen.queryAllByRole("heading", { level: 2 })).toHaveLength(0);
  else expect(headings()[0]).toBe(first);
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
  const rows = [...writerRows, ...["13", "12", "11", "10"].map((date, index) => onDay(run(10 + index, "merged", 10, 20), `2026-10-${date}`))];
  await mount("#/runs/history", fleet(null, rows).fetch);
  for (const label of firstPage) {
    const section = screen.getByRole("region", { name: label });
    expect(within(section).queryByRole("status")).toBeNull();
    expect(within(section).getByRole("list")).toBeTruthy();
  }
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
      if (query.host.base === DEMO) expect(query.error?.cause).toBe(failure);
      else expect(query.error).toBeNull();
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

const dayRun = (id: number) => within(screen.getByRole("list", { name: "Runs on Wed 14 Oct, latest first" }))
  .getAllByRole("listitem").find((row) => row.getAttribute("data-run") === String(id))!;
const dayCells = (id: number) => Array.from(dayRun(id).children) as HTMLElement[];

const HEAD = ["Run", "Ticket", "Title", "Project", "Outcome", "Rounds", "Findings", "Took · estimate", "Change", "Ended"];
const [OUTCOME, ROUNDS, FINDINGS, TOOK, CHANGE, ENDED] = [4, 5, 6, 7, 8, 9];
const SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678";

test("history rows separate ticket and title and name and link the merged run with its outcome and end time", async () => {
  await mount("#/runs/history", fleet([{
    ...run(213, "merged", 26, 30), ticket: "DEMO-215", title: "Stop a run that edits outside its scope",
    rounds: 2, pr_url: "https://github.com/example/demo/pull/483", ended_ms: midnight("2026-10-14") + (19 * 60 + 45) * MINUTE,
  }], []).fetch);
  const link = screen.getByRole("link", {
    name: "Run 213, DEMO-215 Stop a run that edits outside its scope, demo, merged, took 26m against 30m, ended 19:45",
  });
  expect(link.getAttribute("href")).toEndWith("#/runs/demo/213");
  const cells = dayCells(213);
  expect(cells.slice(0, 4).map((cell) => cell.textContent)).toEqual(["213", "DEMO-215", "Stop a run that edits outside its scope", "demo"]);
  expect(within(cells[OUTCOME]!).getByText("merged")).toBeTruthy();
  expect(cells[ROUNDS]!.textContent).toBe("2");
  expect(cells[ENDED]!.textContent).toBe("19:45");
});

test("history draws each run's time box bar green to 80 %, amber to the box and red past it", async () => {
  await mount("#/runs/history", fleet([
    run(224, "merged", 24, 30), run(225, "merged", 25, 30), run(230, "merged", 30, 30), run(231, "merged", 31, 30),
  ], []).fetch);
  const took = (id: number) => dayCells(id)[TOOK]!;
  expect([224, 225, 230, 231].map((id) => took(id).querySelector("[role='progressbar']")!.getAttribute("data-tone"))).toEqual(["ok", "warn", "warn", "over"]);
  expect([224, 225, 230, 231].map((id) => took(id).querySelector("[data-minutes]")!.textContent)).toEqual(["24m / 30m", "25m / 30m", "30m / 30m", "31m / 30m"]);
  expect([224, 225, 230, 231].map((id) => took(id).querySelector("[data-delta]")!.textContent)).toEqual(["−6m", "−5m", "0m", "+1m"]);
});

test("history leaves the bar empty without an estimate, dashes a run without work and drops the 1.5× note", async () => {
  await mount("#/runs/history", fleet([
    run(211, "merged", 20, null), { ...run(209, "merged", 0, 30), working_ms: null },
  ], []).fetch);
  const unestimated = dayCells(211)[TOOK]!;
  expect(unestimated.textContent).toBe("20m / —");
  expect(unestimated.querySelector("[data-delta]")).toBeNull();
  const bar = unestimated.querySelector("[role='progressbar']")!;
  expect([bar.getAttribute("data-tone"), bar.getAttribute("aria-valuenow"), bar.children.length]).toEqual(["none", "0", 0]);
  const knownLink = within(dayRun(211)).getByRole("link", { name: /^Run 211,/ });
  expect(knownLink.getAttribute("aria-label")).toContain(", took 20m,");
  expect(knownLink.getAttribute("aria-label")).not.toContain("against");
  expect(dayCells(209)[TOOK]!.textContent).toBe("–");
  expect(dayCells(209)[TOOK]!.querySelector("[role='progressbar']")).toBeNull();
  expect(within(dayRun(209)).getByRole("link", { name: /^Run 209,/ }).getAttribute("aria-label")).not.toContain("took");
  expect(screen.queryByText(/1\.5×/)).toBeNull();
});

test("history heads a Findings column and dashes a run without findings as it dashes zero rounds", async () => {
  await mount("#/runs/history", fleet([{ ...run(215, "merged", 20, 30), findings: 4 }, { ...run(216, "merged", 20, 30), findings: 0 }], []).fetch);
  const head = document.querySelector("[data-day-run-table] [aria-hidden='true']")!;
  expect(Array.from(head.children, (cell) => cell.textContent)).toEqual(HEAD);
  expect([dayCells(215)[FINDINGS]!.textContent, dayCells(216)[FINDINGS]!.textContent]).toEqual(["4", "–"]);
  expect(dayCells(216)[ROUNDS]!.textContent).toBe("–");
});

test("history puts a merge's size, pull request and commit in Change instead of under its outcome", async () => {
  await mount("#/runs/history", fleet([{
    ...run(217, "merged", 20, 30), additions: 120, deletions: 14, pr_url: "https://github.com/example/demo/pull/812",
    merge_sha: SHA, commit_url: `https://github.com/example/demo/commit/${SHA}`,
  }], []).fetch);
  const change = dayCells(217)[CHANGE]!;
  expect(change.querySelector("[data-size]")!.textContent).toBe("+120 −14");
  const pr = within(change).getByRole("link", { name: "PR #812" });
  const sha = within(change).getByRole("link", { name: SHA.slice(0, 7) });
  expect([pr.getAttribute("href"), pr.getAttribute("target")]).toEqual(["https://github.com/example/demo/pull/812", "_blank"]);
  expect([sha.getAttribute("href"), sha.getAttribute("target")]).toEqual([`https://github.com/example/demo/commit/${SHA}`, "_blank"]);
  expect(change.textContent).toBe(`+120 −14PR #812${SHA.slice(0, 7)}`);
  expect(dayCells(217)[OUTCOME]!.textContent).not.toContain("PR #812");
});

test("history shows a merge without a pull request as its plain short sha and dashes Change on other outcomes", async () => {
  await mount("#/runs/history", fleet([
    { ...run(218, "merged", 20, 30), additions: null, deletions: null, merge_sha: SHA, commit_url: null },
    { ...run(208, "failed", 10, 30), failure_kind: "verify" },
    { ...run(210, "abandoned", 10, 30), outcome_reason: "superseded" },
  ], []).fetch);
  const change = dayCells(218)[CHANGE]!;
  expect(change.textContent).toBe(SHA.slice(0, 7));
  expect(within(change).queryByRole("link")).toBeNull();
  expect([dayCells(208)[CHANGE]!.textContent, dayCells(210)[CHANGE]!.textContent]).toEqual(["–", "–"]);
});

test("history reads rows from a daemon that serves no findings, sha or size", async () => {
  const legacy = { ...run(219, "merged", 20, 30), pr_url: "https://github.com/example/demo/pull/790" };
  for (const key of ["findings", "merge_sha", "commit_url", "additions", "deletions"]) expect(legacy).not.toHaveProperty(key);
  await mount("#/runs/history", fleet([legacy, run(220, "failed", 10, 30)], []).fetch);
  expect(screen.queryByText(/did not answer/)).toBeNull();
  expect([dayCells(219)[FINDINGS]!.textContent, dayCells(220)[FINDINGS]!.textContent]).toEqual(["–", "–"]);
  expect(dayCells(219)[CHANGE]!.textContent).toBe("");
  expect(within(dayCells(219)[CHANGE]!).queryByRole("link")).toBeNull();
});

test("history explains failed and abandoned outcomes and prefixes an end on a later day", async () => {
  await mount("#/runs/history", fleet([
    { ...run(208, "failed", 10, 30), failure_kind: "verify" },
    { ...run(210, "abandoned", 10, 30), outcome_reason: "superseded by a newer run\nrun 214 claimed it",
      ended_ms: midnight("2026-10-15") + 20 * MINUTE },
  ], []).fetch);
  const failed = dayCells(208)[OUTCOME]!;
  expect(within(failed).getByText("failed")).toBeTruthy();
  expect(within(failed).getByText("at verify")).toBeTruthy();
  const abandoned = dayCells(210)[OUTCOME]!;
  expect(within(abandoned).getByText("abandoned")).toBeTruthy();
  expect(within(abandoned).getByText("superseded by a newer run")).toBeTruthy();
  expect(abandoned.textContent).not.toContain("run 214 claimed it");
  expect(dayCells(210)[ENDED]!.textContent).toBe("Thu 00:20");
});

test("history greys a paused run a later run resumed and names that run under it, leaving a paused run with no later run as it was", async () => {
  await mount("#/runs/history", fleet([
    { ...run(220, "paused", 10, 30), phase: "paused", resumed_as: 224 },
    { ...run(222, "paused", 10, 30), phase: "paused", resumed_as: null },
  ], []).fetch);
  const resumed = dayCells(220)[OUTCOME]!;
  expect(within(resumed).getByText("paused").className).toContain("text-ink-3");
  expect(within(resumed).getByText(/^resumed as/).textContent).toBe("resumed as run 224");
  expect(within(resumed).getByRole("link", { name: "run 224" }).getAttribute("href")).toEndWith("#/runs/demo/224");
  const waiting = dayCells(222)[OUTCOME]!;
  expect(within(waiting).getByText("paused").className).toContain("text-ask-ink");
  expect(waiting.textContent).not.toContain("resumed as");
});

const onDay = (row: HistoryRow, date: string): HistoryRow => ({
  ...row, started_ms: midnight(date) + row.id * MINUTE, ended_ms: midnight(date) + (row.id + 60) * MINUTE,
});
const outcomeDemo = [
  ...Array.from({ length: 7 }, (_, index) => onDay(run(101 + index, "merged", 30, 40), "2026-10-14")),
  ...Array.from({ length: 7 }, (_, index) => onDay(run(108 + index, "merged", 30, 40), "2026-10-13")),
  ...Array.from({ length: 3 }, (_, index) => onDay(run(201 + index, "failed", 20, 20), "2026-10-12")),
  ...Array.from({ length: 3 }, (_, index) => onDay(run(204 + index, "failed", 20, 20), "2026-10-11")),
];
const outcomeWriter = [onDay(run(301, "abandoned", 16, 20), "2026-10-10")];

const outcomeButtons = () => within(screen.getByRole("group", { name: "Outcome" }));

test("Outcome counts the whole page while filtering rows, preserving day heads and paging in the link", async () => {
  await mount("#/runs/history", fleet(outcomeDemo, outcomeWriter).fetch);
  for (const name of ["All 21", "Merged 14", "Failed 6", "Abandoned 1"]) {
    expect(outcomeButtons().getByRole("button", { name }).getAttribute("aria-pressed")).toBe(String(name === "All 21"));
  }
  await act(async () => void fireEvent.click(outcomeButtons().getByRole("button", { name: "Failed 6" })));
  await settlePage();
  expect(new URLSearchParams(window.location.hash.split("?")[1]).get("outcome")).toBe("failed");
  expect(outcomeButtons().getByRole("button", { name: "Failed 6" }).getAttribute("aria-pressed")).toBe("true");
  expect(outcomeButtons().getByRole("button", { name: "All 21" })).toBeTruthy();
  const rows = screen.getAllByRole("list", { name: /^Runs on/ }).flatMap((list) => within(list).getAllByRole("listitem"));
  expect(rows).toHaveLength(6);
  expect(rows.map((row) => row.getAttribute("data-run")).sort()).toEqual(["201", "202", "203", "204", "205", "206"]);
  const mergedDay = within(screen.getByRole("region", { name: "Wed 14 Oct" }));
  expect(mergedDay.getByText("No failed runs this day.")).toBeTruthy();
  expect(screen.getByRole("region", { name: "Wed 14 Oct" }).textContent).toContain("7 runs");
  expect(mergedDay.getByText("3h 30m of work against 4h 40m estimated")).toBeTruthy();
  await act(async () => void fireEvent.click(screen.getByRole("button", { name: "Older days" })));
  await settlePage();
  const search = new URLSearchParams(window.location.hash.split("?")[1]);
  expect(search.get("page")).toBe("2");
  expect(search.get("outcome")).toBe("failed");
});

test("an invalid outcome selects All and choosing All removes the filter while keeping project", async () => {
  await mount("#/runs/history?project=demo&outcome=bogus", fleet(outcomeDemo, outcomeWriter).fetch);
  expect(outcomeButtons().getByRole("button", { name: "All 20" }).getAttribute("aria-pressed")).toBe("true");
  expect(outcomeButtons().getByRole("button", { name: "Abandoned 0" })).toBeTruthy();
  await act(async () => void fireEvent.click(outcomeButtons().getByRole("button", { name: "Failed 6" })));
  await settlePage();
  expect(within(screen.getByRole("region", { name: "Wed 14 Oct" })).getByText("No failed runs in demo this day.")).toBeTruthy();
  await act(async () => void fireEvent.click(outcomeButtons().getByRole("button", { name: "All 20" })));
  await settlePage();
  const search = new URLSearchParams(window.location.hash.split("?")[1]);
  expect(search.has("outcome")).toBe(false);
  expect(search.get("project")).toBe("demo");
  expect(screen.getAllByRole("list", { name: /^Runs on/ }).flatMap((list) => within(list).getAllByRole("listitem"))).toHaveLength(20);
});

test.each([
  [1, "No abandoned runs in demo from Oct 14 to Oct 10. Try older days."],
  [6, "No abandoned runs in demo from Sep 19 to Sep 15."],
])("an empty scoped outcome replaces every day on page %s with its range sentence", async (page, sentence) => {
  await mount(`#/runs/history?project=demo&outcome=abandoned&page=${page}`, fleet(outcomeDemo, outcomeWriter).fetch);
  expect(screen.getByText(sentence)).toBeTruthy();
  expect(screen.queryAllByRole("heading", { level: 2 })).toHaveLength(0);
  expect(screen.queryAllByRole("list", { name: /^Runs on/ })).toHaveLength(0);
});

const isDayQuery = (url: string, base: string, date = "2026-10-14") => {
  const parsed = new URL(url);
  return parsed.origin === base && parsed.pathname === "/history"
    && parsed.searchParams.get("since") === String(midnight(date)) && parsed.searchParams.get("limit") === "500";
};

test("a pending host hides the entire day's counts and rows while other days remain visible", async () => {
  const server = fleet(demoRows, [...writerRows, onDay(run(6, "merged", 10, 20), "2026-10-13")]);
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  try {
    await mount("#/runs/history", async (url) => {
      if (isDayQuery(url, DEMO)) await pending;
      return server.fetch(url);
    });
    const day = screen.getByRole("region", { name: "Wed 14 Oct" });
    expect(day.getAttribute("aria-busy")).toBe("true");
    expect(within(day).queryByRole("list")).toBeNull();
    expect(day.textContent).toBe("Wed 14 Oct");
    expect(within(screen.getByRole("region", { name: "Tue 13 Oct" })).getByRole("link", { name: /^Run 6,/ })).toBeTruthy();
    expect(outcomeButtons().getByRole("button", { name: "All 1" })).toBeTruthy();
    await act(async () => void fireEvent.click(outcomeButtons().getByRole("button", { name: "Failed 0" })));
    await settlePage();
    expect(screen.queryByText("No failed runs from Oct 14 to Oct 10. Try older days.")).toBeNull();
    expect(screen.getByRole("region", { name: "Wed 14 Oct" }).getAttribute("aria-busy")).toBe("true");
  } finally {
    release();
    await settlePage();
  }
});

test("a failed day names the project and attempt clock, excludes partial counts, and retries only its failed query", async () => {
  const server = fleet(demoRows, writerRows.slice(0, 2));
  const requests: string[] = [];
  let failing = true;
  await mount("#/runs/history", async (url) => {
    requests.push(url);
    if (failing && isDayQuery(url, DEMO)) return new Response("unavailable", { status: 500 });
    return server.fetch(url);
  }, Date.parse("2026-10-16T16:53:00-07:00"));
  const day = screen.getByRole("region", { name: "Wed 14 Oct" });
  const fault = within(day).getByRole("status");
  expect(fault.textContent).toContain("The run store for demo did not answer.");
  expect(fault.textContent).toContain("asked for Wed 14 Oct at 16:53 · showing nothing rather than part of a day");
  expect(within(day).queryByRole("list")).toBeNull();
  expect(day.textContent).not.toContain("2 runs");
  for (const name of ["All 0", "Merged 0", "Failed 0", "Abandoned 0"]) {
    expect(outcomeButtons().getByRole("button", { name })).toBeTruthy();
  }
  expect(screen.queryByText("No runs from Oct 14 to Oct 10. Try older days.")).toBeNull();
  const beforeRetry = requests.length;
  failing = false;
  await act(async () => void fireEvent.click(within(day).getByRole("button", { name: "Try again" })));
  await settlePage();
  expect(requests.slice(beforeRetry)).toHaveLength(1);
  expect(isDayQuery(requests[beforeRetry]!, DEMO)).toBe(true);
  expect(within(day).queryByRole("status")).toBeNull();
  expect(within(day).getAllByRole("listitem")).toHaveLength(4);
  for (const id of [1, 2, 3, 4]) expect(within(day).getByRole("link", { name: new RegExp(`^Run ${id},`) })).toBeTruthy();
  expect(outcomeButtons().getByRole("button", { name: "All 4" })).toBeTruthy();
});

test("a cut day's head reports the answer's limit and total and sums only cut hosts", async () => {
  const cutRows = (date: string) => Array.from({ length: 500 }, (_, index) => onDay(run(index + 1, "merged", 30, 40), date));
  const server = fleet([...cutRows("2026-10-13"), ...cutRows("2026-10-12")], cutRows("2026-10-12"));
  await mount("#/runs/history", async (url) => {
    const response = await server.fetch(url);
    if (isDayQuery(url, DEMO, "2026-10-13")) {
      return Response.json({ ...await response.json(), truncated: true, limit: 500, total: 612 });
    }
    if (isDayQuery(url, DEMO, "2026-10-12") || isDayQuery(url, WRITER, "2026-10-12")) {
      return Response.json({ ...await response.json(), truncated: true, limit: 500, total: new URL(url).origin === DEMO ? 612 : 700 });
    }
    return response;
  });
  expect(within(screen.getByRole("region", { name: "Tue 13 Oct" })).getByText("newest 500 of 612 shown")).toBeTruthy();
  expect(within(screen.getByRole("region", { name: "Mon 12 Oct" })).getByText("newest 1000 of 1312 shown")).toBeTruthy();
});

test("an unknown project scope settles to the empty page sentence without busy days", async () => {
  await mount("#/runs/history?project=unknown", fleet().fetch);
  expect(screen.getByText("No runs in unknown from Oct 14 to Oct 10. Try older days.")).toBeTruthy();
  expect(screen.queryAllByRole("heading", { level: 2 })).toHaveLength(0);
  expect(document.querySelector('[aria-busy="true"]')).toBeNull();
});

test("an empty host daemon leaves the working project's days and Outcome counts visible", async () => {
  const host = await Bun.file(new URL("../../../tests/fixtures/serve/host-status.json", import.meta.url)).json();
  const server = fleet();
  await mount("#/runs/history", async (url) => {
    if (url === `${DEMO}/status`) return Response.json({ ...host, projects: [] });
    return server.fetch(url);
  });
  for (const label of firstPage) {
    expect(screen.getByRole("region", { name: label }).getAttribute("aria-busy")).toBe("false");
  }
  const day = within(screen.getByRole("region", { name: "Wed 14 Oct" }));
  expect(day.getAllByRole("listitem")).toHaveLength(3);
  expect(day.getByRole("link", { name: /^Run 3,/ })).toBeTruthy();
  expect(outcomeButtons().getByRole("button", { name: "All 3" })).toBeTruthy();
  expect(outcomeButtons().getByRole("button", { name: "Merged 1" })).toBeTruthy();
});
