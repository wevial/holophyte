import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { Attention, RunDetail, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { addressOf } from "../../src/lib/hosts";
import { storeToken } from "../../src/lib/token";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const TOKEN = "tok-286";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const detail = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const ready = await Bun.file(new URL("run-merge-ready.json", SERVE)).json();
const served = (await Bun.file(new URL("attention.json", SERVE)).json()) as Attention;

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const NOW = new Date(2026, 9, 15, 16, 30).getTime();
const at = (hour: number, minute: number) => new Date(2026, 9, 15, hour, minute).getTime();

type Item = Attention["items"][number];
const failed = (triage: unknown): Item => ({
  kind: "failed", level: "attention", run: 12, ticket: "DEMO-11", ticket_url: null, reason: "verify failed: 2 tests red",
  ended_ms: NOW - (8 * HOUR + 2 * MINUTE), attempt: 2, pr_url: null, triage,
} as Item);
const blocked = (ticket: string, run: number, askedAgo: number, extra: object = {}): Item => ({
  kind: "blocked", level: "attention", ticket, ticket_url: null, question: "Which way?", run,
  asked_ms: NOW - askedAgo, pr_url: null, park_kind: null, pr: null, ...extra,
} as Item);
const code = { choice: "code", confidence: 0.81, backend: "claude", model: "triage-model", requeued: false };

interface Daemon {
  items: Item[];
  runs?: Status["runs"];
  details?: Record<number, RunDetail>;
  actions?: boolean;
  /** Answers every `POST /actions/...`. */
  act?: Fetch;
  /** Every URL the daemon was asked for, in order. */
  asked?: string[];
}

/** One project daemon whose bodies the test may change between polls. */
function daemon(state: Daemon): Fetch {
  return async (url, init) => {
    state.asked?.push(url);
    if (url.includes("/actions/") && state.act) return state.act(url, init);
    const merge = url.match(/\/runs\/(\d+)\/merge$/);
    if (merge) return Response.json({ ...ready, run: Number(merge[1]) });
    const match = url.match(/\/runs\/(\d+)$/);
    if (match) {
      const body = state.details?.[Number(match[1])];
      return body ? Response.json(body) : new Response("no such run", { status: 404 });
    }
    return peersFetch(DEMO, {
      [DEMO]: {
        status: { ...working, actions: state.actions ?? false, thresholds: { ...working.thresholds, strikes: 3 }, now: NOW, project: "/srv/dev/demo", runs: state.runs ?? [], queued: [] },
        attention: { level: state.items.length ? "attention" : "none", now: NOW, project: "/srv/dev/demo", items: state.items },
      },
    })(url);
  };
}

let client: QueryClient | null = null;

async function mount(fetchImpl: Fetch, clockMs = NOW) {
  window.location.hash = "#/";
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = clockMs;
  client = consoleQueryClient();
  render(
    <QueryClientProvider client={client}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
  return fake;
}

async function repoll() {
  await act(async () => {
    await client!.refetchQueries({ queryKey: ["fleet"] });
    await settle();
  });
  for (let round = 0; round < 2; round += 1) await act(settle);
}

const waitRows = () => [...document.querySelectorAll<HTMLElement>("li[data-wait-row]")];
const rows = () => waitRows().map((row) => row.dataset.waitRow);
const row = (key: string) => document.querySelector<HTMLElement>(`li[data-wait-row="${key}"]`)!;
const toggleOf = (key: string) => row(key).querySelector<HTMLButtonElement>(":scope > div > button")!;
const openKeys = () => waitRows().filter((li) => toggleOf(li.dataset.waitRow!).getAttribute("aria-expanded") === "true").map((li) => li.dataset.waitRow);
const openRow = () => document.querySelector<HTMLElement>("li[data-wait-row][data-open]")!;
const openDetail = () => document.getElementById(toggleOf(openRow().dataset.waitRow!).getAttribute("aria-controls")!)!;
const openDetailText = () => openDetail().textContent ?? "";
const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => storeToken(addressOf(DEMO), TOKEN));

afterEach(() => {
  localStorage.clear();
  cleanup();
  client?.clear();
  client = null;
  window.location.hash = "";
});

test("a failed item's row names its state, key and age, and its open detail its strike, or its bare why without triage", async () => {
  await mount(daemon({ items: [failed(code), blocked("DEMO-4", 4, HOUR)] }));
  expect(rows()).toEqual(["DEMO-11", "DEMO-4"]);
  expect(within(row("DEMO-11")).getByRole("img", { name: "failed" })).toBeTruthy();
  expect(toggleOf("DEMO-11").textContent).toEndWith("failed · 8h");
  expect(within(openDetail()).getByText("run #12 · strike 2 of 3")).toBeTruthy();
  cleanup();

  await mount(daemon({ items: [failed(null), blocked("DEMO-4", 4, HOUR)] }));
  expect(within(openDetail()).getByText("Verify command failed")).toBeTruthy();
});

test("the open detail lists the run's three newest events but bookkeeping, newest first", async () => {
  const events = [
    { at: at(15, 10), kind: "phase_change", summary: "claimed -> working" },
    { at: at(16, 2), kind: "verify", summary: "verify failed: 2 tests red" },
    { at: at(15, 40), kind: "review", summary: "round 1 changes requested" },
    { at: at(16, 20), kind: "pr_seen_commits", summary: "saw 1 commit" },
    { at: at(15, 55), kind: "phase_change", summary: "working -> verifying" },
  ];
  await mount(daemon({ items: [failed(code)], details: { 12: { ...detail, events } } }));
  const lines = within(openDetail()).getByRole("list", { name: "Latest progress" }).querySelectorAll("li");
  expect([...lines].map((line) => `${line.querySelector("time")?.textContent}${line.querySelector("p")?.textContent}`)).toEqual([
    "16:02verify failed: 2 tests red",
    "15:55working -> verifying",
    "15:40round 1 changes requested",
  ]);
  expect(openDetailText()).toContain("No pull request yet");
});

test("an untitled item's row takes its live run's title, else the open run detail's", async () => {
  const live = { ...working.runs[0]!, id: 4, ticket: "DEMO-4", title: "Cache the board read" };
  await mount(daemon({ items: [blocked("DEMO-4", 4, HOUR)], runs: [live], details: { 4: { ...detail, run: { ...detail.run, title: "Stale title" } } } }));
  expect(toggleOf("DEMO-4").textContent).toContain("Cache the board read");
  expect(toggleOf("DEMO-4").textContent).not.toContain("Stale title");
  cleanup();

  await mount(daemon({ items: [blocked("DEMO-4", 4, HOUR)], details: { 4: { ...detail, run: { ...detail.run, title: "Retry the board sync" } } } }));
  expect(toggleOf("DEMO-4").textContent).toContain("Retry the board sync");
});

test("a pinned question park behind an older failure reads its filed title in its row with no live run", async () => {
  const question = { ...served.items.find((item) => item.kind === "blocked")!, asked_ms: NOW - HOUR } as Item;
  await mount(daemon({ items: [question, failed(code)] }));
  expect(rows()).toEqual(["DEMO-11", "KO-14"]);
  expect(toggleOf("KO-14").textContent).toContain("Pick the export format");
});

const titled = (ticket: string, run: number, askedAgo: number, extra: object = {}) =>
  blocked(ticket, run, askedAgo, { park_kind: "question", title: `Title of ${ticket}`, ...extra });

test("Waiting on you draws a row per entry, oldest first with the oldest open, and no card, pager or Later", async () => {
  await mount(daemon({ items: [titled("C-3", 3, HOUR), titled("A-1", 1, 3 * HOUR), titled("B-2", 2, 2 * HOUR)] }));
  expect(rows()).toEqual(["A-1", "B-2", "C-3"]);
  for (const [key, age] of [["A-1", "3h"], ["B-2", "2h"], ["C-3", "1h"]] as const) {
    const line = toggleOf(key);
    expect(within(line).getByRole("img", { name: "question" })).toBeTruthy();
    expect([...line.children].map((part) => part.textContent)).toEqual(["", key, `Title of ${key}`, `question · ${age}`]);
  }
  expect(openKeys()).toEqual(["A-1"]);
  expect(screen.queryByRole("article", { name: "Focus" })).toBeNull();
  expect(screen.queryByText(/1 of 3/)).toBeNull();
  for (const name of ["Previous item", "Next item", "Later, show the next one"]) expect(screen.queryByRole("button", { name })).toBeNull();
});

test("pressing a row's toggle opens it alone, and pressing the open one leaves no row open", async () => {
  await mount(daemon({ items: [titled("A-1", 1, 3 * HOUR), titled("B-2", 2, 2 * HOUR), titled("C-3", 3, HOUR)] }));
  fireEvent.click(toggleOf("B-2"));
  expect(openKeys()).toEqual(["B-2"]);
  fireEvent.click(toggleOf("B-2"));
  expect(openKeys()).toEqual([]);
  expect(document.querySelector("li[data-wait-row][data-open]")).toBeNull();
  fireEvent.click(toggleOf("C-3"));
  expect(openKeys()).toEqual(["C-3"]);
});

test("an open question row's detail holds its headline, ask, latest events and actions; a closed row draws no detail", async () => {
  const events = [{ at: at(16, 2), kind: "review", summary: "round 1 changes requested" }];
  await mount(daemon({ items: [titled("A-1", 1, 3 * HOUR), titled("B-2", 2, 2 * HOUR)], actions: true, details: { 1: { ...detail, events } } }));
  const open = openDetail();
  expect(within(open).getByRole("heading", { name: "The run has a question for you" })).toBeTruthy();
  expect(within(open).getByText("Which way?")).toBeTruthy();
  expect(within(open).getByRole("list", { name: "Latest progress" }).textContent).toContain("round 1 changes requested");
  expect(within(open).getByRole("button", { name: "Answer" }).dataset.variant).toBe("primary");
  fireEvent.click(within(open).getByRole("button", { name: "More" }));
  expect(within(screen.getByRole("group", { name: "More actions" })).getByRole("button", { name: "Requeue" })).toBeTruthy();
  expect(within(open).getByRole("link", { name: "Open run" }).getAttribute("href")).toEndWith("#/runs/demo/1");
  expect(document.getElementById(toggleOf("B-2").getAttribute("aria-controls")!)).toBeNull();
});

test("a failed row's open detail ends its why with triage's verdict, and a stuck wait's reads its dependency and queue", async () => {
  const stuck = {
    kind: "stuck_wait", level: "attention", ticket: "DEMO-5", ticket_url: null, title: "Ship the export", dependency: "DEMO-2", dependency_state: "abandoned", reason: null, behind: 4,
  } as Item;
  await mount(daemon({ items: [failed(code), stuck] }));
  expect(rows()).toEqual(["DEMO-11", "DEMO-5"]);
  expect(within(openDetail()).getByText(/^Verify command failed/).textContent).toEndWith("Triage says it is the code, 81%.");
  fireEvent.click(toggleOf("DEMO-5"));
  expect(within(openDetail()).getByText("Waits on DEMO-2, which was abandoned")).toBeTruthy();
  expect(within(openDetail()).getByText("4 queued behind")).toBeTruthy();
});

test("a poll that puts an older item first leaves the open row open", async () => {
  const b = titled("B-2", 2, 2 * HOUR);
  const c = titled("C-3", 3, HOUR);
  const state: Daemon = { items: [b, c] };
  await mount(daemon(state));
  expect(openKeys()).toEqual(["B-2"]);
  state.items = [titled("A-1", 1, 3 * HOUR), b, c];
  await repoll();
  expect(rows()).toEqual(["A-1", "B-2", "C-3"]);
  expect(openKeys()).toEqual(["B-2"]);
});

test("when a poll drops the open row's item the oldest remaining row opens", async () => {
  const a = titled("A-1", 1, 3 * HOUR);
  const b = titled("B-2", 2, 2 * HOUR);
  const c = titled("C-3", 3, HOUR);
  const state: Daemon = { items: [a, b, c] };
  await mount(daemon(state));
  fireEvent.click(toggleOf("B-2"));
  state.items = [a, c];
  await repoll();
  expect(openKeys()).toEqual(["A-1"]);
});

test("an open Waiting on you row opens on the needs-you wash and an open Pull requests row on the act wash", async () => {
  await mount(daemon({ items: [titled("A-1", 1, 3 * HOUR), titled("B-2", 2, 2 * HOUR), park], details: { 1: { ...detail, events: [] }, 9: { ...detail, events: [] } } }));
  const prRow = document.querySelector<HTMLElement>("li[data-pr-row]")!;
  fireEvent.click(within(prRow).getByRole("button", { expanded: false }));
  for (const [li, wash, rule] of [[row("A-1"), "need-wash", "need"], [prRow, "act-wash", "act"]] as const) {
    expect(li.hasAttribute("data-open")).toBe(true);
    expect(li.className.split(/\s+/)).toEqual(expect.arrayContaining([`data-open:bg-${wash}`, `data-open:shadow-[inset_2px_0_0_var(--${rule})]`]));
  }
  expect(row("B-2").hasAttribute("data-open")).toBe(false);
});

test("a row with a pull request links it as #N beside its toggle without toggling; a row without one shows no link", async () => {
  const pr = "https://github.com/example/demo/pull/2405";
  await mount(daemon({ items: [titled("A-1", 1, 3 * HOUR), titled("B-2", 2, 2 * HOUR, { pr_url: pr })] }));
  const link = within(row("B-2")).getByRole("link", { name: "#2405" });
  expect(link.getAttribute("href")).toBe(pr);
  expect(link.getAttribute("target")).toBe("_blank");
  expect(toggleOf("B-2").contains(link)).toBe(false);
  fireEvent.click(link);
  expect(openKeys()).toEqual(["A-1"]);
  expect(within(row("A-1")).queryByRole("link", { name: /^#/ })).toBeNull();
});

test("an unreachable host's open detail says it is not answering and when it was last seen, with no PR line or events", async () => {
  let down = false;
  const up = daemon({ items: [] });
  const host = { ...working, host: "host-a", now: NOW, project: "/srv/dev/demo", runs: [], queued: [] };
  const fake = await mount(async (url) => {
    if (down && !url.endsWith("/peers")) throw new Error("connection refused");
    if (url.endsWith("/status")) return Response.json(host);
    return up(url);
  }, NOW - 4 * MINUTE);
  down = true;
  fake.clock.now = NOW;
  await repoll();
  expect(within(openDetail()).getByText("host-a is not answering")).toBeTruthy();
  expect(within(openDetail()).getByText("last seen 4m ago")).toBeTruthy();
  expect(openDetailText()).not.toContain("PR");
  expect(openDetailText()).not.toContain("pull request");
  expect(within(openDetail()).queryByRole("list", { name: "Latest progress" })).toBeNull();
});

const TICKET_URL = "https://linear.app/demo/issue/DEMO-11";
const failedAt = (extra: object = {}): Item => ({ ...failed(code), ticket_url: TICKET_URL, ...extra } as Item);
const stale = {
  kind: "stale_run", level: "attention", run: 7, ticket: "DEMO-7", ticket_url: null, phase: "working", heartbeat_age_ms: 20 * MINUTE,
  pr_url: "https://github.com/example/demo/pull/40",
} as Item;
const park = {
  kind: "pr_open", level: "attention", ticket: "DEMO-9", ticket_url: null, title: "Link each run to its pull request", run: 9,
  park_kind: "pull_request", asked_ms: NOW - HOUR, reason: "PR open", pr_url: "https://github.com/example/demo/pull/42",
  pr: { number: 42, checks: "success", review: "approved", threads: 0, open_threads: 0, title: "Link each run", state: "ready" },
} as Item;
const paused = {
  kind: "paused", level: "attention", ticket: "DEMO-6", ticket_url: null, title: "Hold the migration", note: "hold", run: 6, asked_ms: NOW - HOUR, pr_url: null,
} as Item;

const inDetail = (name: string) => within(openDetail()).getByRole("button", { name }) as HTMLButtonElement;
const toast = () => document.querySelector("[data-toast]")?.textContent ?? "";
const attentionReads = (asked: string[]) => asked.filter((url) => url.endsWith("/attention")).length;
const press = async (button: HTMLElement) => {
  await act(async () => {
    fireEvent.click(button);
  });
  for (let round = 0; round < 3; round += 1) await act(settle);
};

test("pressing Open run on a failed run's open row opens that run's Run page", async () => {
  const run40 = { ...detail, run: { ...detail.run, id: 40, ticket: "DEMO-40", title: "Write the notes", phase: "failed", outcome: "failed" } };
  await mount(daemon({ items: [{ ...failed(null), run: 40, ticket: "DEMO-40" } as Item], details: { 40: run40 } }));
  await press(within(openDetail()).getByRole("link", { name: "Open run" }));
  expect(window.location.hash).toStartWith("#/runs/demo/40");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Write the notes");
});

test("the open row's primary is the entry's first action, the rest sit under More, and Open run and Open ticket follow", async () => {
  await mount(daemon({ items: [failedAt()], actions: true }));
  const primary = inDetail("Requeue");
  expect(primary.dataset.variant).toBe("primary");
  expect(within(openDetail()).queryByRole("button", { name: "Mark needs_spec" })).toBeNull();
  const more = inDetail("More");
  fireEvent.click(more);
  expect(more.getAttribute("aria-expanded")).toBe("true");
  const needsSpec = within(screen.getByRole("group", { name: "More actions" })).getByRole("button", { name: "Mark needs_spec" });
  expect(needsSpec.hasAttribute("disabled")).toBe(true);
  expect(needsSpec.getAttribute("title")).toBe("not wired yet");
  fireEvent.keyDown(needsSpec, { key: "Escape" });
  expect(screen.queryByRole("group", { name: "More actions" })).toBeNull();
  expect(document.activeElement).toBe(more);
  expect(within(openDetail()).getByRole("link", { name: "Open run" }).getAttribute("href")).toEndWith("#/runs/demo/12");
  const ticket = within(openDetail()).getByRole("link", { name: "Open ticket" });
  expect(ticket.getAttribute("href")).toBe(TICKET_URL);
  expect(ticket.getAttribute("target")).toBe("_blank");
  cleanup();

  await mount(daemon({ items: [stale], actions: true }));
  expect(inDetail("Abort").dataset.variant).toBe("danger");
  fireEvent.click(inDetail("More"));
  const staleMore = within(screen.getByRole("group", { name: "More actions" })).getAllByRole("button");
  expect(staleMore.map((button) => button.textContent)).toEqual(["Abort and close", "Requeue"]);
});

test("a host without actions draws no button in the open row, says so, and still links the run", async () => {
  await mount(daemon({ items: [failedAt()] }));
  expect(within(openDetail()).queryAllByRole("button")).toEqual([]);
  expect(openDetailText()).toContain("Actions are off on this host");
  expect(within(openDetail()).getByRole("link", { name: "Open run" })).toBeTruthy();
});

test("a taken requeue says so in the toast and re-reads the fleet at once; a refused one shows the daemon's reason in the open row", async () => {
  const asked: string[] = [];
  const state: Daemon = { items: [failedAt(), blocked("DEMO-4", 4, HOUR)], actions: true, asked };
  const taken = fakeFetch({ ok: true, detail: "requeued" });
  state.act = async (url, init) => {
    state.items = [blocked("DEMO-4", 4, HOUR)];
    return taken.fetchImpl(url, init);
  };
  await mount(daemon(state));
  const reads = attentionReads(asked);
  await press(inDetail("Requeue"));
  expect(taken.seen.map(({ url, body }) => ({ url, body }))).toEqual([{ url: `${DEMO}/actions/requeue`, body: { ticket: "DEMO-11" } }]);
  expect(screen.getAllByRole("status").find((region) => region.getAttribute("aria-live") === "polite")?.textContent).toBe("Requeued DEMO-11.");
  expect(attentionReads(asked)).toBeGreaterThan(reads);
  expect(openRow().dataset.waitRow).toBe("DEMO-4");
  cleanup();

  const refused = fakeFetch({ ok: false, detail: "ticket DEMO-11 has a live run" });
  await mount(daemon({ items: [failedAt()], actions: true, act: refused.fetchImpl }));
  await press(inDetail("Requeue"));
  expect(openRow().dataset.waitRow).toBe("DEMO-11");
  expect(within(openDetail()).getByRole("status").textContent).toBe("ticket DEMO-11 has a live run");
  expect(toast()).toBe("");
});

test("Resume opens a reason box in the open row whose Send posts the ticket and the note once, then toasts and re-reads the fleet", async () => {
  const asked: string[] = [];
  const action = fakeFetch({ ok: true, detail: "resumed" });
  await mount(daemon({ items: [paused], actions: true, act: action.fetchImpl, asked }));
  const reads = attentionReads(asked);
  await press(inDetail("Resume"));
  fireEvent.change(within(openDetail()).getByRole("textbox", { name: "Reason to resume" }), { target: { value: "window is open" } });
  await press(inDetail("Send"));
  expect(action.seen.map(({ url, body }) => ({ url, body }))).toEqual([{ url: `${DEMO}/actions/resume`, body: { ticket: "DEMO-6", note: "window is open" } }]);
  expect(toast()).toBe("Resumed DEMO-6.");
  expect(attentionReads(asked)).toBeGreaterThan(reads);
});

test("Abort and close posts the run, close and the reason, and adds pr_comment only when its optional box holds text", async () => {
  const abortClose = async (comment: string | null) => {
    const action = fakeFetch({ ok: true, detail: "aborted" });
    await mount(daemon({ items: [stale], actions: true, act: action.fetchImpl }));
    fireEvent.click(inDetail("More"));
    await press(within(screen.getByRole("group", { name: "More actions" })).getByRole("button", { name: "Abort and close" }));
    fireEvent.change(within(openDetail()).getByRole("textbox", { name: "Reason to abort and close" }), { target: { value: "wrong approach" } });
    const box = within(openDetail()).getByRole("textbox", { name: "Comment on the PR (optional)" }) as HTMLTextAreaElement;
    expect(box.value).toBe("");
    if (comment != null) fireEvent.change(box, { target: { value: comment } });
    await press(inDetail("Send"));
    cleanup();
    return action.seen.map(({ url, body }) => ({ url, body }));
  };
  expect(await abortClose(null)).toEqual([{ url: `${DEMO}/actions/abort`, body: { run: 7, close: true, note: "wrong approach" } }]);
  expect(await abortClose("Superseded.")).toEqual([
    { url: `${DEMO}/actions/abort`, body: { run: 7, close: true, pr_comment: "Superseded.", note: "wrong approach" } },
  ]);
});

test("a host sweep row on a host with no named projects reads the root's actions and posts Run sweep at the root", async () => {
  const hostStatus = await Bun.file(new URL("host-status.json", SERVE)).json();
  const action = fakeFetch({ ok: true, detail: "started" });
  const sweep = { kind: "sweep_stale", project: null, state: "killed", started: NOW - 200_000, ended: null, level: "attention" };
  await mount(async (url, init) => {
    if (url === `${DEMO}/peers`) return Response.json({ self: addressOf(DEMO), peers: [] });
    if (url === `${DEMO}/status`) return Response.json({ ...hostStatus, now: NOW, actions: true, projects: [] });
    if (url === `${DEMO}/attention`) return Response.json({ level: "attention", now: NOW, items: [sweep] });
    if (url.includes("/actions/")) return action.fetchImpl(url, init);
    return new Response("not found", { status: 404 });
  });
  expect(openDetailText()).not.toContain("Actions are off");
  await press(inDetail("Run sweep"));
  expect(action.seen.map(({ url }) => url)).toEqual([`${DEMO}/actions/run-sweep`]);
  expect(toast()).toBe("Started a host sweep on writer-host.");
});

const prUrl = "https://github.com/example/repo/pull/644";
const parkedQuestion = `PR open: ${prUrl}\nchecks failure on the head commit; Fix round made no commit; operator instruction needed:\nI couldn't reproduce the \`unit\` failure.\n\n- **The CI log:** unavailable\n- **The full suite:** passed`;

const fixDeclined = (extra: object = {}) => blocked("DEMO-4", 4, HOUR, {
  park_kind: "fix_declined", pr: { number: 644, checks: "failure", review: null, threads: null, open_threads: null, title: null, state: "ready" }, pr_url: prUrl, question: parkedQuestion, ...extra,
});

test("a declined check fix leads with structured facts and renders the ask without machine prefixes", async () => {
  await mount(daemon({ items: [fixDeclined()] }));
  expect(within(openDetail()).getByRole("heading", { name: "Checks failed on PR #644 and the fix round found nothing to change" })).toBeTruthy();
  expect(within(openDetail()).getByRole("link", { name: "PR #644" }).getAttribute("href")).toBe(prUrl);
  expect(openDetail().querySelector("code")?.textContent).toBe("unit");
  expect(openDetail().querySelector("strong")?.textContent).toBe("The CI log:");
  expect(openDetail().querySelectorAll("li")).toHaveLength(2);
  expect(openDetailText()).not.toContain("PR open:");
  expect(openDetailText()).not.toContain("Fix round made no commit");
});

test("a question without a pull request has a generic headline and no PR link", async () => {
  await mount(daemon({ items: [blocked("DEMO-4", 4, HOUR, { park_kind: "question", pr: null, pr_url: null })] }));
  expect(within(openDetail()).getByRole("heading", { name: "The run has a question for you" })).toBeTruthy();
  expect(within(openDetail()).queryByRole("link", { name: /PR/ })).toBeNull();
});

test("the ask displays raw HTML literally and expands from four lines", async () => {
  await mount(daemon({ items: [blocked("DEMO-4", 4, HOUR, { question: "<script>alert(1)</script>" })] }));
  expect(openDetail().querySelector("script")).toBeNull();
  expect(within(openDetail()).getByText("<script>alert(1)</script>")).toBeTruthy();
  const folded = openDetail().querySelector(".line-clamp-4")!;
  expect(folded).toBeTruthy();
  const toggle = within(openDetail()).getByRole("button", { name: "Show all" });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(toggle);
  expect(folded.classList.contains("line-clamp-4")).toBe(false);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(toggle.textContent).toBe("Show less");
  fireEvent.click(toggle);
  expect(folded.classList.contains("line-clamp-4")).toBe(true);
});

test("latest event Markdown expands independently from two lines", async () => {
  await mount(daemon({ items: [fixDeclined()], details: { 4: { ...detail, events: [{ at: NOW, kind: "phase_change", summary: "**Parked** on `unit`" }] } } }));
  const entry = within(openDetail()).getByRole("list", { name: "Latest progress" }).firstElementChild!;
  expect(entry.querySelector("strong")?.textContent).toBe("Parked");
  expect(entry.querySelector("code")?.textContent).toBe("unit");
  const folded = entry.querySelector(".line-clamp-2")!;
  expect(folded).toBeTruthy();
  const toggle = within(entry as HTMLElement).getByRole("button", { name: "Show all" });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(folded.classList.contains("line-clamp-2")).toBe(false);
  expect(openDetail().querySelector(".line-clamp-4")).toBeTruthy();
});

test.each([
  [{ pr: null }, "The fix round on PR #644 found nothing to change"],
  [{ pr: null, pr_url: null }, "The fix round found nothing to change"],
  [{ park_kind: null }, "The run has a question for you"],
])("the blocked headline falls back using available structured facts (%j)", async (extra, headline) => {
  await mount(daemon({ items: [fixDeclined(extra)] }));
  expect(within(openDetail()).getByRole("heading", { name: headline })).toBeTruthy();
});

test("ask links use safe URLs and unsupported headings and images retain their text", async () => {
  await mount(daemon({ items: [blocked("DEMO-4", 4, HOUR, {
    question: "# Read this\n\n![Important instruction](https://example.com/image.png)\n\n![Reference instruction][image]\n\n[image]: https://example.com/image.png\n\n[Log](https://example.com/log) and [unsafe](javascript:alert%281%29)",
  })] }));
  expect(within(openDetail()).getByText("Read this")).toBeTruthy();
  expect(within(openDetail()).queryByRole("heading", { name: "Read this" })).toBeNull();
  expect(within(openDetail()).getByText("Important instruction")).toBeTruthy();
  expect(within(openDetail()).getByText("Reference instruction")).toBeTruthy();
  expect(openDetail().querySelector("img")).toBeNull();
  const log = within(openDetail()).getByRole("link", { name: "Log" });
  expect(log.getAttribute("href")).toBe("https://example.com/log");
  expect(log.getAttribute("target")).toBe("_blank");
  expect(log.getAttribute("rel")).toBe("noreferrer");
  expect(within(openDetail()).getByText("unsafe").getAttribute("href")).toBe("");
});
