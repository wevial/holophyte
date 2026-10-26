import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, renderHook, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { addressOf } from "../../src/lib/hosts";
import type { Status as PolledStatus } from "../../src/lib/types";
import type { ActiveOutage, DayLogPrefs, LedgerEntry, ShippedRow, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { EVERYTHING } from "../../src/app/dayLog";
import { consoleQueryClient } from "../../src/app/fleet";
import { useDayLedger } from "../../src/app/queries";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, hostOf, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const WRITER = "http://writer-host:7711";
const RELOS = "http://runner-host:7710";
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

const entry = (time: number, kind: string, source: string, ticket: string | null, text: string, extra: Partial<LedgerEntry> = {}): LedgerEntry => ({
  at: time, run: null, ticket, pr_url: null, kind, source, text, ...extra,
});

const DEMO_PR = "https://github.com/example/demo/pull/521";
const PARK_PR = "https://github.com/example/demo/pull/518";
const SWEPT_PR = "https://github.com/example/writer/pull/77";
const REQUEUE_PR = "https://github.com/example/writer/pull/76";
const PARK_TEXT = "implementer: criterion C7 cannot be met as written: the export has no header field";

const demoDay = [
  entry(at(11, 15), "merge", "loop", "DEMO-10", "MERGED to main as 5acc138 (branch task/demo-10 deleted).", { run: 10, pr_url: DEMO_PR }),
  entry(at(11, 5), "round", "loop", "DEMO-10", `Babysit pass 1 over ${DEMO_PR}: no unresolved threads, checks success`, { run: 10, pr_url: DEMO_PR }),
  entry(at(10, 50), "round", "loop", "DEMO-10", "Round 2: APPROVE\nReviewer verdict:\nVERDICT: APPROVE", { run: 10, pr_url: DEMO_PR }),
  entry(at(10, 20), "round", "loop", "DEMO-10", "Round 1: REQUEST_CHANGES -> fix round\nReviewer findings:\n- p1 the limit", { run: 10, pr_url: DEMO_PR }),
  entry(at(10, 0), "note", "loop", "DEMO-10", `PR OPEN: ${DEMO_PR}\nchecks pending\nBranch task/demo-10 is pushed at 5acc138`, { run: 10, pr_url: DEMO_PR }),
  entry(at(9, 40), "park", "factory", "DEMO-4", PARK_TEXT, { run: 4, pr_url: PARK_PR }),
];
const writerDay = [
  entry(at(10, 30), "failure", "factory", "WRITER-6", "no heartbeat for 20 minutes", { run: 6, pr_url: SWEPT_PR, failure_kind: "swept" }),
  entry(at(9, 0), "intervention", "operator", "WRITER-5", "human requeue: retry after the flaky check", { run: 5, pr_url: REQUEUE_PR }),
];
const HIGHLIGHT_LINES = ["11:15 DEMO-10 merged", "10:30 WRITER-6 failed: swept", "09:40 DEMO-4 needs you: C7 contradiction", "09:00 WRITER-5 operator requeued"];
const ALL_LINES = [
  "11:15 DEMO-10 merged",
  "11:05 DEMO-10 babysit pass 1: checks green",
  "10:50 DEMO-10 review round 2: approve",
  "10:30 WRITER-6 failed: swept",
  "10:20 DEMO-10 review round 1: changes asked",
  "10:00 DEMO-10 PR opened",
  "09:40 DEMO-4 needs you: C7 contradiction",
  "09:00 WRITER-5 operator requeued",
];

type Answer = () => Response | Promise<Response>;
const ledgerOf = (...entries: LedgerEntry[]): Answer => () =>
  Response.json({ entries: [...entries].sort((a, b) => b.at - a.at), active_outages: [], since: MIDNIGHT, limit: 1000 });
const notFound: Answer = () => new Response("not found", { status: 404 });
const prefsOf = (dayLog: DayLogPrefs | null): Answer => () => Response.json({ day_log: dayLog, updated_ms: null });

interface Daemon {
  shipped?: ShippedRow[];
  ledger?: Answer;
  colour?: Status["colour"];
}

interface Origin {
  prefs?: Answer;
  put?: Answer;
}

/** The daemons, each answering its `/ledger` from `ledger`, and the origin's
 *  `/prefs`; every `/ledger` url asked and every `PUT /prefs` body is kept. */
function fleet(demo: Daemon, writer: Daemon, origin: Origin = {}, relos?: Daemon) {
  const asked: string[] = [];
  const puts: unknown[] = [];
  const daemons: [string, string, Daemon][] = [[DEMO, "/srv/dev/demo", demo], [WRITER, "/srv/dev/writer", writer]];
  if (relos) daemons.push([RELOS, "/srv/dev/relos", relos]);
  const bodies = (project: string, colour: Daemon["colour"]) => ({
    status: { ...working, project, runs: [], queued: [], colour: colour ?? "auto" },
    attention: { level: "none", now: NOW, project, items: [] },
  });
  const polls = peersFetch(DEMO, Object.fromEntries(daemons.map(([base, project, daemon]) => [base, bodies(project, daemon.colour)])));
  const fetch: Fetch = async (url, init) => {
    if (url === `${DEMO}/prefs`) {
      if (init?.method !== "PUT") return (origin.prefs ?? prefsOf(null))();
      puts.push(JSON.parse(String(init.body)));
      return (origin.put ?? prefsOf(null))();
    }
    for (const [base, , daemon] of daemons) {
      if (url === `${base}/shipped?limit=50`) return Response.json({ rows: daemon.shipped ?? [], next_before: null, limit: 50 });
      if (url.startsWith(`${base}/ledger?`)) {
        asked.push(url);
        return (daemon.ledger ?? ledgerOf())();
      }
    }
    return polls(url);
  };
  return { fetch, asked, puts };
}

const fixtureDay = (origin: Origin = {}) => fleet({ ledger: ledgerOf(...demoDay) }, { ledger: ledgerOf(...writerDay) }, origin);

async function mount(hash: string, fetchImpl: Fetch) {
  window.location.hash = hash;
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = NOW;
  const queries = consoleQueryClient();
  render(
    <QueryClientProvider client={queries}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
  return queries;
}

const mergeRows = () => within(screen.getByRole("region", { name: "Merged today" })).getAllByRole("listitem");
const log = () => screen.getByRole("complementary", { name: "Today's log" });
const entryRows = () => within(log()).queryAllByRole("listitem").filter((row) => row.querySelector("time") != null);
const lineOf = (row: HTMLElement) => {
  const key = row.querySelector('a[href*="/tickets/"]')?.textContent;
  return [row.querySelector("time")!.textContent, key, row.querySelector("[data-phrase]")!.textContent].filter(Boolean).join(" ");
};
const logLines = () => within(log()).queryAllByRole("listitem").map((row) => (row.querySelector("time") ? lineOf(row) : row.textContent));
const hashOf = (href: string) => (href.includes("#/") ? href.slice(href.indexOf("#/")) : href);
const tones = () => Array.from(log().querySelectorAll("[data-tone]"), (mark) => mark.getAttribute("data-tone"));
const toggle = () => within(log()).queryByRole("button", { name: /^(Show all|Highlights only)/ });
const box = (name: string) => screen.getByRole("checkbox", { name }) as HTMLInputElement;

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("Merged today lists each host's merges newest first, with the pull request or short sha and rounds", async () => {
  const { fetch } = fleet({ shipped: [demo10] }, { shipped: [writer4] });
  localStorage.setItem("holophyte.next.fold.done", "open");
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

test("with no saved filter the log lists only the merge, failure, park and requeue rows newest first, asked from local midnight", async () => {
  const { fetch, asked } = fixtureDay();
  await mount("#/", fetch);
  expect(logLines()).toEqual(HIGHLIGHT_LINES);
  expect(tones()).toEqual(["live", "bad", "ask", "act"]);
  expect(toggle()?.textContent).toBe("Show all (4 hidden)");
  expect(toggle()?.getAttribute("aria-pressed")).toBe("false");
  for (const base of [DEMO, WRITER]) {
    const url = new URL(asked.find((made) => made.startsWith(`${base}/`))!);
    expect(url.pathname).toBe("/ledger");
    expect(url.searchParams.get("since")).toBe(String(MIDNIGHT));
    expect(url.searchParams.has("kind")).toBe(false);
  }
});

test("each row links its key to the ticket page, its PR chip to the pull request and its run chip to the run page, with no URL as text", async () => {
  await mount("#/", fixtureDay().fetch);
  const links = entryRows().map((row) => within(row).getAllByRole("link").map((link) => [link.textContent, hashOf(link.getAttribute("href")!)]));
  expect(links).toEqual([
    [["DEMO-10", "#/tickets/demo/DEMO-10"], ["#521", DEMO_PR], ["run 10", "#/runs/demo/10"]],
    [["WRITER-6", "#/tickets/writer/WRITER-6"], ["#77", SWEPT_PR], ["run 6", "#/runs/writer/6"]],
    [["DEMO-4", "#/tickets/demo/DEMO-4"], ["#518", PARK_PR], ["run 4", "#/runs/demo/4"]],
    [["WRITER-5", "#/tickets/writer/WRITER-5"], ["#76", REQUEUE_PR], ["run 5", "#/runs/writer/5"]],
  ]);
  expect(entryRows().map((row) => within(row).getByRole("link", { name: /^#/ }).getAttribute("target"))).toEqual(["_blank", "_blank", "_blank", "_blank"]);
  expect(entryRows().filter((row) => row.textContent!.includes("https://"))).toEqual([]);
});

test("Show all lists every row with the rest in the quiet tone, and Highlights only returns to the four", async () => {
  await mount("#/", fixtureDay().fetch);
  await act(async () => toggle()!.click());
  expect(logLines()).toEqual(ALL_LINES);
  expect(toggle()?.textContent).toBe("Highlights only");
  expect(toggle()?.getAttribute("aria-pressed")).toBe("true");
  const quiet = entryRows().filter((row) => row.hasAttribute("data-quiet")).map(lineOf);
  expect(quiet).toEqual(["11:05 DEMO-10 babysit pass 1: checks green", "10:50 DEMO-10 review round 2: approve", "10:20 DEMO-10 review round 1: changes asked", "10:00 DEMO-10 PR opened"]);
  for (const row of entryRows().filter((row) => row.hasAttribute("data-quiet"))) {
    expect(row.className).toContain("text-ink-3");
    expect(row.querySelector("[data-tone]")!.getAttribute("data-tone")).toBe("ink-3");
  }

  await act(async () => toggle()!.click());
  expect(logLines()).toEqual(HIGHLIGHT_LINES);
});

test("each project's keys take its status colour, or the palette's colour at its place in the project order when auto", async () => {
  const row = (ticket: string, minute: number) => ledgerOf(entry(at(11, minute), "merge", "loop", ticket, "MERGED to main as 5acc138."));
  const { fetch } = fleet({ ledger: row("DEMO-1", 3), colour: "auto" }, { ledger: row("WRITER-1", 2), colour: "teal" }, {}, { ledger: row("RELOS-1", 1), colour: "auto" });
  await mount("#/", fetch);
  const colours = entryRows().map((row) => {
    const key = row.querySelector<HTMLElement>('a[href*="/tickets/"]')!;
    return [key.textContent, key.style.color];
  });
  expect(colours).toEqual([["DEMO-1", "var(--p-violet)"], ["WRITER-1", "var(--p-teal)"], ["RELOS-1", "var(--p-green)"]]);
});

test("a saved manual filter adds its items and drops cleared projects; Everything shows every row with no Show all", async () => {
  const manual = (items: Record<string, boolean>, projects: Record<string, boolean> = {}): DayLogPrefs => ({ preset: "manual", items, projects });
  await mount("#/", fixtureDay({ prefs: prefsOf(manual({ verdicts: true })) }).fetch);
  expect(logLines()).toEqual(ALL_LINES.filter((line) => !/babysit|PR opened/.test(line)));
  cleanup();

  await mount("#/", fixtureDay({ prefs: prefsOf(manual({}, { writer: false })) }).fetch);
  expect(logLines()).toEqual(["11:15 DEMO-10 merged", "09:40 DEMO-4 needs you: C7 contradiction"]);
  cleanup();

  await mount("#/", fixtureDay({ prefs: prefsOf(EVERYTHING) }).fetch);
  expect(logLines()).toEqual(ALL_LINES);
  expect(toggle()).toBeNull();
});

test("an origin answering GET /prefs with 404 or failing leaves the log on Highlights", async () => {
  const failing: Answer = () => {
    throw new Error("connection refused");
  };
  for (const prefs of [notFound, failing]) {
    await mount("#/", fixtureDay({ prefs }).fetch);
    expect(logLines()).toEqual(HIGHLIGHT_LINES);
    cleanup();
    await mount("#/settings/log", fixtureDay({ prefs }).fetch);
    expect(screen.getByRole("button", { name: "Highlights" }).getAttribute("aria-pressed")).toBe("true");
    expect(box("Verdicts").checked).toBe(false);
    cleanup();
  }
});

test("a refused PUT /prefs says so in a toast with the reason and the choice stays on the page", async () => {
  const put: Answer = () => Response.json({ error: "day_log.items: more than 64 entries" }, { status: 400 });
  await mount("#/settings/log", fixtureDay({ put }).fetch);
  await act(async () => fireEvent.click(box("Verdicts")));
  await act(settle);
  expect(document.querySelector("[data-toast]")?.textContent).toBe("Today's log filter not saved: day_log.items: more than 64 entries");
  expect(box("Verdicts").checked).toBe(true);

  await act(async () => screen.getByRole("link", { name: "Back" }).click());
  await act(settle);
  expect(logLines()).toContain("10:50 DEMO-10 review round 2: approve");
});

test("a tick made while GET /prefs is still answering outlives that answer", async () => {
  let answer: (response: Response) => void = () => {};
  const pending: Answer = () => new Promise((resolve) => (answer = resolve));
  await mount("#/settings/log", fixtureDay({ prefs: pending }).fetch);
  await act(async () => fireEvent.click(box("Verdicts")));
  await act(async () => {
    answer(Response.json({ day_log: null, updated_ms: null }));
    await settle();
  });
  await act(async () => screen.getByRole("link", { name: "Back" }).click());
  await act(settle);
  expect(logLines()).toContain("10:50 DEMO-10 review round 2: approve");
});

test("the log caps at 25 lines of the filtered list with a count of the rest, skips a host without the route, and says when it is empty", async () => {
  const steers = Array.from({ length: 30 }, (_, index) => entry(at(1, 0) + index * 60_000, "intervention", "operator", "DEMO-7", `human steer: step ${index}`));
  const rounds = Array.from({ length: 10 }, (_, index) => entry(at(2, 0) + index * 60_000, "round", "loop", "DEMO-7", `Round ${index + 1}: APPROVE`));
  await mount("#/", fleet({ ledger: ledgerOf(...steers, ...rounds) }, {}).fetch);
  const lines = logLines();
  expect(lines).toHaveLength(26);
  expect(lines[0]).toBe("01:29 DEMO-7 operator steered");
  expect(lines.at(-1)).toBe("and 5 earlier today");
  cleanup();

  await mount(
    "#/",
    fleet({ ledger: notFound }, { ledger: ledgerOf(entry(at(9, 0), "merge", "loop", "WRITER-4", "MERGED to main as 3c9a2f1.")) }).fetch,
  );
  expect(logLines()).toEqual(["09:00 WRITER-4 merged"]);
  cleanup();

  await mount("#/", fleet({}, {}).fetch);
  expect(logLines()).toEqual([]);
  expect(log().textContent).toContain("Nothing logged since midnight.");
});

test("a row's phrase opens in place to the entry's full text, code block included, and closes again", async () => {
  const failed = "capture failed for DEMO-7\n\n```\n$ bun run capture /srv/dev/demo/console\n┌─ 01-board.png\n└─ timeout\n```";
  await mount("#/", fleet({ ledger: ledgerOf(entry(at(11, 0), "failure", "loop", "DEMO-7", failed)) }, {}).fetch);
  const opener = within(log()).getByRole("button", { name: "failed" });
  expect(opener.className).toContain("line-clamp-2");
  expect(log().textContent).not.toContain("timeout");

  await act(async () => opener.click());
  expect(opener.getAttribute("aria-expanded")).toBe("true");
  expect(log().querySelector("pre")?.textContent).toBe(failed);

  await act(async () => opener.click());
  expect(opener.getAttribute("aria-expanded")).toBe("false");
  expect(log().textContent).not.toContain("timeout");
});

/** happy-dom lays nothing out, so every element gets a stand-in layout: a
 *  40-character column at 18px a line, cut to two lines under `line-clamp-2`,
 *  and no size once detached. */
async function wrapAt40(run: () => Promise<void>) {
  const line = 18;
  const lines = (element: HTMLElement) => (element.isConnected ? Math.max(1, Math.ceil((element.textContent ?? "").length / 40)) : 0);
  const saved = ["scrollHeight", "clientHeight"].map((name) => [name, Object.getOwnPropertyDescriptor(HTMLElement.prototype, name)] as const);
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return lines(this) * line;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return Math.min(lines(this), this.classList.contains("line-clamp-2") ? 2 : Infinity) * line;
    },
  });
  try {
    await run();
  } finally {
    for (const [name, descriptor] of saved) {
      if (descriptor) Object.defineProperty(HTMLElement.prototype, name, descriptor);
      else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[name];
    }
  }
}

/** A ResizeObserver whose callbacks run when `resizeAll` is called, as a browser runs them after layout. */
function manualResizeObserver() {
  const live = new Set<() => void>();
  const saved = globalThis.ResizeObserver;
  globalThis.ResizeObserver = class {
    #fire: () => void;
    constructor(callback: ResizeObserverCallback) {
      this.#fire = () => callback([], this as unknown as ResizeObserver);
    }
    observe() {
      live.add(this.#fire);
    }
    unobserve() {
      live.delete(this.#fire);
    }
    disconnect() {
      live.delete(this.#fire);
    }
  } as unknown as typeof ResizeObserver;
  return {
    resizeAll: () => act(() => live.forEach((fire) => fire())),
    restore: () => {
      globalThis.ResizeObserver = saved;
    },
  };
}

test("an Other phrase wrapping past the two-line clamp opens to its full text, still after a resize", async () => {
  const long = Array.from({ length: 30 }, (_, index) => `word${index}`).join(" ");
  const observers = manualResizeObserver();
  try {
    await wrapAt40(async () => {
      const other = prefsOf({ preset: "manual", items: { other: true }, projects: {} });
      await mount("#/", fleet({ ledger: ledgerOf(entry(at(11, 0), "note", "loop", "DEMO-9", long)) }, {}, { prefs: other }).fetch);
      observers.resizeAll();
      const opener = within(log()).getByRole("button", { name: long });

      await act(async () => opener.click());
      expect(log().querySelector("pre")?.textContent).toBe(long);
    });
  } finally {
    observers.restore();
  }
});

test("an opened entry stays open when a newer entry arrives above it", async () => {
  const failed = "capture failed for DEMO-7\n\n```\n└─ timeout\n```";
  const first = entry(at(11, 0), "failure", "loop", "DEMO-7", failed);
  let answer = ledgerOf(first);
  const queries = await mount("#/", fleet({ ledger: () => answer() }, {}).fetch);
  await act(async () => within(log()).getByRole("button", { name: "failed" }).click());

  answer = ledgerOf(first, entry(at(11, 30), "merge", "loop", "DEMO-10", "MERGED to main as 5acc138."));
  await act(async () => {
    await queries.refetchQueries();
    await settle();
  });

  expect(logLines()[0]).toBe("11:30 DEMO-10 merged");
  expect(within(log()).getByRole("button", { name: "failed" }).getAttribute("aria-expanded")).toBe("true");
  expect(log().textContent).toContain("└─ timeout");
});

test("a host whose ledger read fails after a good answer keeps its lines", async () => {
  let answer = ledgerOf(entry(at(11, 15), "merge", "loop", "DEMO-10", "MERGED to main as 5acc138."));
  const { fetch, asked } = fleet({ ledger: () => answer() }, {});
  const queries = await mount("#/", fetch);
  const demoAsks = () => asked.filter((url) => url.startsWith(`${DEMO}/`)).length;
  const before = demoAsks();
  expect(logLines()).toEqual(["11:15 DEMO-10 merged"]);

  answer = () => new Response("boom", { status: 500 });
  await act(async () => {
    await queries.refetchQueries();
    await settle();
  });

  expect(demoAsks()).toBeGreaterThan(before);
  expect(logLines()).toEqual(["11:15 DEMO-10 merged"]);
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
