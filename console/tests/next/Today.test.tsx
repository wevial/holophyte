import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, render, renderHook, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { addressOf } from "../../src/lib/hosts";
import type { Status as PolledStatus } from "../../src/lib/types";
import type { ActiveOutage, LedgerEntry, ShippedRow, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { useDayLedger } from "../../src/next/queries";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, hostOf, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const WRITER = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const polled = (await Bun.file(new URL("status.json", SERVE)).json()) as PolledStatus;
const shippedRow = ((await Bun.file(new URL("shipped.json", SERVE)).json()) as { rows: ShippedRow[] }).rows[0]!;

const NOW = new Date(2026, 9, 15, 12, 0).getTime();
const MIDNIGHT = new Date(2026, 9, 15, 0, 0).getTime();
const at = (hour: number, minute: number) => new Date(2026, 9, 15, hour, minute).getTime();

const merged = (row: Partial<ShippedRow>): ShippedRow => ({ ...shippedRow, outcome: "merged", ...row });
const demo10 = merged({
  id: 10, ticket: "DEMO-10", title: "Page the shipped ledger", rounds: 1, ended_ms: at(11, 15),
  pr_url: "https://github.com/example/demo/pull/521", merge_sha: "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
});
const writer4 = merged({
  id: 4, ticket: "WRITER-4", title: "Write the release notes", rounds: 2, ended_ms: at(8, 47),
  pr_url: null, merge_sha: "3c9a2f1d0e4b5a6978877665544332211aabbccd", commit_url: null,
});

const entry = (time: number, kind: string, source: string, ticket: string | null, text: string): LedgerEntry => ({
  at: time, run: null, ticket, kind, source, text,
});

type Answer = () => Response;
const ledgerOf = (...entries: LedgerEntry[]): Answer => () =>
  Response.json({ entries: [...entries].sort((a, b) => b.at - a.at), active_outages: [], since: MIDNIGHT, limit: 1000 });
const notFound: Answer = () => new Response("not found", { status: 404 });

interface Daemon {
  shipped?: ShippedRow[];
  ledger?: Answer;
}

/** The two daemons, each answering its `/ledger` from `ledger`; every `/ledger` url asked is kept. */
function fleet(demo: Daemon, writer: Daemon) {
  const asked: string[] = [];
  const bodies = (project: string) => ({
    status: { ...working, project, runs: [], queued: [] },
    attention: { level: "none", now: NOW, project, items: [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: bodies("/srv/dev/demo"), [WRITER]: bodies("/srv/dev/writer") });
  const fetch: Fetch = async (url) => {
    for (const [base, daemon] of [[DEMO, demo], [WRITER, writer]] as const) {
      if (url === `${base}/shipped?limit=50`) return Response.json({ rows: daemon.shipped ?? [], next_before: null, limit: 50 });
      if (url.startsWith(`${base}/ledger?`)) {
        asked.push(url);
        return (daemon.ledger ?? ledgerOf())();
      }
    }
    return polls(url);
  };
  return { fetch, asked };
}

async function mount(hash: string, fetchImpl: Fetch) {
  window.location.hash = hash;
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = NOW;
  const queries = consoleQueryClient();
  render(
    <QueryClientProvider client={queries}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
  return queries;
}

const mergeRows = () => within(screen.getByRole("region", { name: "Merged today" })).getAllByRole("listitem");
const log = () => screen.getByRole("complementary", { name: "Today's log" });
const logLines = () => within(log()).queryAllByRole("listitem").map((line) => line.textContent);
const tones = () => Array.from(log().querySelectorAll("[data-tone]"), (mark) => mark.getAttribute("data-tone"));

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("Merged today lists each host's merges newest first, with the pull request or short sha and rounds", async () => {
  const { fetch } = fleet({ shipped: [demo10] }, { shipped: [writer4] });
  await mount("#/", fetch);
  const [first, second] = mergeRows();
  expect(mergeRows()).toHaveLength(2);
  expect(first!.textContent).toContain("DEMO-10");
  expect(first!.textContent).toContain("PR #521 · 1 round");
  expect(first!.textContent).toContain("11:15");
  expect(second!.textContent).toContain("WRITER-4");
  expect(second!.textContent).toContain("3c9a2f1 · 2 rounds");
  expect(second!.textContent).toContain("08:47");
  cleanup();

  await mount("#/?project=demo", fetch);
  expect(mergeRows().map((row) => row.textContent?.includes("DEMO-10"))).toEqual([true]);
});

test("the day's log folds both hosts' entries newest first, toned by kind, asked from local midnight of every kind", async () => {
  const { fetch, asked } = fleet(
    {
      ledger: ledgerOf(
        entry(at(11, 15), "merge", "loop", "DEMO-10", "MERGED to main as 5acc138."),
        entry(at(10, 0), "intervention", "operator", "DEMO-4", "answered: merge it"),
        entry(at(8, 5), "round", "loop", "DEMO-7", "review round 2"),
      ),
    },
    {
      ledger: ledgerOf(
        entry(at(10, 30), "failure", "loop", "WRITER-6", "verify failed"),
        entry(at(9, 0), "intervention", "loop", null, "requeued after a restart"),
      ),
    },
  );
  await mount("#/", fetch);
  expect(logLines()).toEqual([
    "11:15DEMO-10 · MERGED to main as 5acc138.",
    "10:30WRITER-6 · verify failed",
    "10:00DEMO-4 · answered: merge it",
    "09:00requeued after a restart",
    "08:05DEMO-7 · review round 2",
  ]);
  expect(tones()).toEqual(["live", "bad", "act", "ink-3", "ink-3"]);
  for (const base of [DEMO, WRITER]) {
    const url = new URL(asked.find((made) => made.startsWith(`${base}/`))!);
    expect(url.pathname).toBe("/ledger");
    expect(url.searchParams.get("since")).toBe(String(MIDNIGHT));
    expect(url.searchParams.has("kind")).toBe(false);
  }
});

test("the log caps at 40 lines with a count of the rest, skips a host without the route, and says when it is empty", async () => {
  const many = Array.from({ length: 45 }, (_, index) => entry(at(1, 0) + index * 60_000, "round", "loop", "DEMO-7", `step ${index}`));
  await mount("#/", fleet({ ledger: ledgerOf(...many) }, {}).fetch);
  const lines = logLines();
  expect(lines).toHaveLength(41);
  expect(lines[0]).toBe("01:44DEMO-7 · step 44");
  expect(lines.at(-1)).toBe("and 5 earlier today");
  cleanup();

  await mount(
    "#/",
    fleet({ ledger: notFound }, { ledger: ledgerOf(entry(at(9, 0), "merge", "loop", "WRITER-4", "MERGED to main as 3c9a2f1.")) }).fetch,
  );
  expect(logLines()).toEqual(["09:00WRITER-4 · MERGED to main as 3c9a2f1."]);
  cleanup();

  await mount("#/", fleet({}, {}).fetch);
  expect(logLines()).toEqual([]);
  expect(log().textContent).toContain("Nothing logged since midnight.");
});

test("a host whose ledger read fails after a good answer keeps its lines", async () => {
  let answer = ledgerOf(entry(at(11, 15), "merge", "loop", "DEMO-10", "MERGED to main as 5acc138."));
  const { fetch, asked } = fleet({ ledger: () => answer() }, {});
  const queries = await mount("#/", fetch);
  const demoAsks = () => asked.filter((url) => url.startsWith(`${DEMO}/`)).length;
  const before = demoAsks();
  expect(logLines()).toEqual(["11:15DEMO-10 · MERGED to main as 5acc138."]);

  answer = () => new Response("boom", { status: 500 });
  await act(async () => {
    await queries.refetchQueries();
    await settle();
  });

  expect(demoAsks()).toBeGreaterThan(before);
  expect(logLines()).toEqual(["11:15DEMO-10 · MERGED to main as 5acc138."]);
});

test("each host's whole ledger answer stays readable by its key beside the folded entries, outages included", async () => {
  const outage: ActiveOutage = { project: 1, run: null, ticket: null, kind: "route_down", source: "loop", at: at(9, 0), reason: "probe failed", text: "reviewer route down" };
  const quiet = () => Response.json({ entries: [], active_outages: [outage], since: MIDNIGHT, limit: 1000 });
  const { fetch } = fleet({ ledger: quiet }, { ledger: ledgerOf(entry(at(10, 0), "merge", "loop", "WRITER-4", "MERGED to main as 3c9a2f1.")) });
  const hosts = [DEMO, WRITER].map((base) => hostOf(polled, { level: "none", now: NOW, items: [] }, base));
  const queries = consoleQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={queries}>{children}</QueryClientProvider>;
  const hook = renderHook(() => useDayLedger(hosts, NOW, { fetch }), { wrapper });
  await act(settle);
  await act(settle);

  expect(hook.result.current.entries.map((line) => line.ticket)).toEqual(["WRITER-4"]);
  expect(hook.result.current.answers.get(addressOf(DEMO))?.active_outages).toEqual([outage]);
  expect(hook.result.current.answers.get(addressOf(WRITER))?.entries).toHaveLength(1);
  queries.clear();
});
