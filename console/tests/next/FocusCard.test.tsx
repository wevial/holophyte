import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { Attention, RunDetail, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { addressOf } from "../../src/lib/hosts";
import { storeToken } from "../../src/lib/token";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const TOKEN = "tok-286";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const detail = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const ready = await Bun.file(new URL("run-merge-ready.json", SERVE)).json();

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
      <NextConsole base={DEMO} pollDeps={fake.deps} />
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

const card = () => screen.getByRole("article", { name: "Focus" });
const cardText = () => card().textContent ?? "";
const rows = () => within(screen.getByRole("list", { name: "Then" })).getAllByRole("button").map((row) => row.getAttribute("aria-label"));

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => storeToken(addressOf(DEMO), TOKEN));

afterEach(() => {
  localStorage.clear();
  cleanup();
  client?.clear();
  client = null;
  window.location.hash = "";
});

test("a failed item's card names its state, key and wait, its why ends with triage's verdict, and the rest are rows", async () => {
  await mount(daemon({ items: [failed(code), blocked("DEMO-4", 4, HOUR)] }));
  expect(within(card()).getByRole("img", { name: "failed" })).toBeTruthy();
  expect(cardText()).toContain("DEMO-11");
  expect(cardText()).toContain("waiting 8h 02m");
  expect(within(card()).getByText("Verify command failed. Triage says it is the code, 81%.")).toBeTruthy();
  expect(within(card()).getByText("run #12 · strike 2 of 3")).toBeTruthy();
  expect(rows()).toEqual(["Show DEMO-4"]);
  cleanup();

  await mount(daemon({ items: [failed(null), blocked("DEMO-4", 4, HOUR)] }));
  expect(within(card()).getByText("Verify command failed")).toBeTruthy();
});

test("the card lists the run's three newest events but bookkeeping, newest first, and a parked PR's facts", async () => {
  const events = [
    { at: at(15, 10), kind: "phase_change", summary: "claimed -> working" },
    { at: at(16, 2), kind: "verify", summary: "verify failed: 2 tests red" },
    { at: at(15, 40), kind: "review", summary: "round 1 changes requested" },
    { at: at(16, 20), kind: "pr_seen_commits", summary: "saw 1 commit" },
    { at: at(15, 55), kind: "phase_change", summary: "working -> verifying" },
  ];
  await mount(daemon({ items: [failed(code)], details: { 12: { ...detail, events } } }));
  const lines = within(card()).getByRole("list", { name: "Latest progress" }).querySelectorAll("li");
  expect([...lines].map((line) => `${line.querySelector("time")?.textContent}${line.querySelector("p")?.textContent}`)).toEqual([
    "16:02verify failed: 2 tests red",
    "15:55working -> verifying",
    "15:40round 1 changes requested",
  ]);
  expect(cardText()).toContain("No pull request yet");
  cleanup();

  const park = {
    kind: "pr_open", level: "attention", ticket: "DEMO-9", ticket_url: null, title: "Link each run to its pull request", run: 9, park_kind: "pull_request", asked_ms: NOW - HOUR,
    reason: "PR open", pr_url: "https://github.com/example/demo/pull/42",
    pr: { state: null, number: 42, checks: "success", review: "approved", threads: 0, open_threads: 0, title: "Link each run" },
  } as Item;
  await mount(daemon({ items: [park], details: { 9: { ...detail, events: [] } } }));
  expect(card().querySelector("[data-pr-line]")?.textContent).toBe("PR #42 · checks green · approved · no open threads");
});

test("an untitled item takes its live run's title, else the run detail's", async () => {
  const live = { ...working.runs[0]!, id: 4, ticket: "DEMO-4", title: "Cache the board read" };
  await mount(daemon({ items: [blocked("DEMO-4", 4, HOUR)], runs: [live], details: { 4: { ...detail, run: { ...detail.run, title: "Stale title" } } } }));
  expect(within(card()).getByRole("heading", { level: 3 }).textContent).toBe("Cache the board read");
  cleanup();

  await mount(daemon({ items: [blocked("DEMO-4", 4, HOUR)], details: { 4: { ...detail, run: { ...detail.run, title: "Retry the board sync" } } } }));
  expect(within(card()).getByRole("heading", { level: 3 }).textContent).toBe("Retry the board sync");
});

test("the pager, Later and a row each move the card through the entries", async () => {
  await mount(daemon({ items: [blocked("A-1", 1, 3 * HOUR), blocked("B-2", 2, 2 * HOUR), blocked("C-3", 3, HOUR)] }));
  expect(cardText()).toContain("A-1");
  expect(cardText()).toContain("1 of 3");
  expect(within(card()).getByRole("button", { name: "Previous item" }).hasAttribute("disabled")).toBe(true);

  fireEvent.click(within(card()).getByRole("button", { name: "Next item" }));
  expect(cardText()).toContain("B-2");
  expect(cardText()).toContain("2 of 3");
  expect(rows()).toEqual(["Show A-1", "Show C-3"]);

  fireEvent.click(screen.getByRole("button", { name: "Later, show the next one" }));
  expect(cardText()).toContain("C-3");
  expect(within(card()).getByRole("button", { name: "Next item" }).hasAttribute("disabled")).toBe(true);
  expect(screen.queryByRole("button", { name: "Later, show the next one" })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Show A-1" }));
  expect(cardText()).toContain("A-1");
  expect(cardText()).toContain("1 of 3");
});

test("when a poll drops the card's item the card shows the one that followed, or the one before the last", async () => {
  const a = blocked("A-1", 1, 3 * HOUR);
  const b = blocked("B-2", 2, 2 * HOUR);
  const c = blocked("C-3", 3, HOUR);
  const state: Daemon = { items: [a, b, c] };
  await mount(daemon(state));
  fireEvent.click(screen.getByRole("button", { name: "Show B-2" }));
  state.items = [a, c];
  await repoll();
  expect(cardText()).toContain("C-3");
  expect(cardText()).toContain("2 of 2");
  cleanup();

  state.items = [a, b, c];
  await mount(daemon(state));
  fireEvent.click(screen.getByRole("button", { name: "Show C-3" }));
  state.items = [a, b];
  await repoll();
  expect(cardText()).toContain("B-2");
  expect(cardText()).toContain("2 of 2");
});

test("a poll that puts an older item first leaves the card on the item it showed", async () => {
  const b = blocked("B-2", 2, 2 * HOUR);
  const state: Daemon = { items: [b] };
  await mount(daemon(state));
  expect(cardText()).toContain("B-2");
  state.items = [blocked("A-1", 1, 3 * HOUR), b];
  await repoll();
  expect(cardText()).toContain("B-2");
  expect(cardText()).toContain("2 of 2");
});

test("an unreachable host's card says it is not answering and when it was last seen, with no PR line or events", async () => {
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
  expect(within(card()).getByRole("heading", { level: 3 }).textContent).toBe("host-a is not answering");
  expect(within(card()).getByText("last seen 4m ago")).toBeTruthy();
  expect(cardText()).not.toContain("PR");
  expect(cardText()).not.toContain("pull request");
  expect(within(card()).queryByRole("list", { name: "Latest progress" })).toBeNull();
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
  pr: { state: null, number: 42, checks: "success", review: "approved", threads: 0, open_threads: 0, title: "Link each run" },
} as Item;
const paused = {
  kind: "paused", level: "attention", ticket: "DEMO-6", ticket_url: null, title: "Hold the migration", note: "hold", run: 6, asked_ms: NOW - HOUR, pr_url: null,
} as Item;

const inCard = (name: string) => within(card()).getByRole("button", { name }) as HTMLButtonElement;
const toast = () => document.querySelector("[data-toast]")?.textContent ?? "";
const attentionReads = (asked: string[]) => asked.filter((url) => url.endsWith("/attention")).length;
const press = async (button: HTMLElement) => {
  await act(async () => {
    fireEvent.click(button);
  });
  for (let round = 0; round < 3; round += 1) await act(settle);
};

test("pressing Open run on a failed run's card opens that run's Run page", async () => {
  const run40 = { ...detail, run: { ...detail.run, id: 40, ticket: "DEMO-40", title: "Write the notes", phase: "failed", outcome: "failed" } };
  await mount(daemon({ items: [{ ...failed(null), run: 40, ticket: "DEMO-40" } as Item], details: { 40: run40 } }));
  await press(within(card()).getByRole("link", { name: "Open run" }));
  expect(window.location.hash).toStartWith("#/runs/demo/40");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Write the notes");
});

test("the card's primary is the entry's first action, the rest sit under More, and Open run and Open ticket follow", async () => {
  await mount(daemon({ items: [failedAt()], actions: true }));
  const primary = inCard("Requeue");
  expect(primary.dataset.variant).toBe("primary");
  expect(within(card()).queryByRole("button", { name: "Mark needs_spec" })).toBeNull();
  const more = inCard("More");
  fireEvent.click(more);
  expect(more.getAttribute("aria-expanded")).toBe("true");
  const needsSpec = within(screen.getByRole("group", { name: "More actions" })).getByRole("button", { name: "Mark needs_spec" });
  expect(needsSpec.hasAttribute("disabled")).toBe(true);
  expect(needsSpec.getAttribute("title")).toBe("not wired yet");
  fireEvent.keyDown(needsSpec, { key: "Escape" });
  expect(screen.queryByRole("group", { name: "More actions" })).toBeNull();
  expect(document.activeElement).toBe(more);
  expect(within(card()).getByRole("link", { name: "Open run" }).getAttribute("href")).toEndWith("#/runs/demo/12");
  const ticket = within(card()).getByRole("link", { name: "Open ticket" });
  expect(ticket.getAttribute("href")).toBe(TICKET_URL);
  expect(ticket.getAttribute("target")).toBe("_blank");
  cleanup();

  await mount(daemon({ items: [stale], actions: true }));
  expect(inCard("Abort").dataset.variant).toBe("danger");
  fireEvent.click(inCard("More"));
  const staleMore = within(screen.getByRole("group", { name: "More actions" })).getAllByRole("button");
  expect(staleMore.map((button) => button.textContent)).toEqual(["Abort and close", "Requeue"]);
  cleanup();

  await mount(daemon({ items: [park], actions: true, details: { 9: { ...detail, events: [] } } }));
  expect(inCard("Merge").dataset.variant).toBe("primary");
  fireEvent.click(inCard("More"));
  expect(within(screen.getByRole("group", { name: "More actions" })).getAllByRole("button").map((button) => button.textContent)).toEqual(["Open PR"]);
});

test("Merge asks first in a dialog listing why it is safe; Esc posts nothing and Confirm posts the run once with the bearer", async () => {
  const action = fakeFetch({ ok: true, detail: "approved" });
  await mount(daemon({ items: [park], actions: true, act: action.fetchImpl, details: { 9: { ...detail, events: [] } } }));
  const merge = inCard("Merge");
  await press(merge);
  const dialog = screen.getByRole("dialog", { name: "Merge PR #42?" });
  const facts = within(dialog).getByRole("list", { name: "Why it is safe" }).querySelectorAll("li");
  expect([...facts].map((fact) => fact.textContent)).toEqual((ready.facts as { detail: string }[]).map((fact) => fact.detail));
  expect(facts).toHaveLength(8);
  expect(dialog.textContent).toContain("The console cannot undo a merge");
  const cancel = within(dialog).getByRole("button", { name: "Cancel" });
  const confirm = within(dialog).getByRole("button", { name: "Merge PR #42" });
  expect(document.activeElement).toBe(cancel);
  fireEvent.keyDown(cancel, { key: "Tab" });
  expect(document.activeElement).toBe(confirm);
  fireEvent.keyDown(confirm, { key: "Tab" });
  expect(document.activeElement).toBe(cancel);
  fireEvent.keyDown(cancel, { key: "Tab", shiftKey: true });
  expect(document.activeElement).toBe(confirm);
  fireEvent.keyDown(document.activeElement!, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(merge);
  expect(action.seen).toEqual([]);

  await press(merge);
  await press(within(screen.getByRole("dialog")).getByRole("button", { name: "Merge PR #42" }));
  expect(action.seen.map(({ url, body, authorization }) => ({ url, body, authorization }))).toEqual([
    { url: `${DEMO}/actions/merge`, body: { run: 9 }, authorization: `Bearer ${TOKEN}` },
  ]);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(toast()).toBe("Approved PR #42. The loop merges it on its next claim.");
});

test("a refused merge keeps the dialog open with the daemon's reason and its confirm pressable again", async () => {
  const action = fakeFetch({ ok: false, detail: "GitHub's review decision is REVIEW_REQUIRED" });
  await mount(daemon({ items: [park], actions: true, act: action.fetchImpl, details: { 9: { ...detail, events: [] } } }));
  await press(inCard("Merge"));
  await press(within(screen.getByRole("dialog")).getByRole("button", { name: "Merge PR #42" }));
  const dialog = screen.getByRole("dialog", { name: "Merge PR #42?" });
  expect(within(dialog).getByRole("alert").textContent).toBe("GitHub's review decision is REVIEW_REQUIRED");
  const confirm = within(dialog).getByRole("button", { name: "Merge PR #42" });
  expect(confirm.hasAttribute("disabled")).toBe(false);
  expect(confirm.getAttribute("aria-busy")).toBe("false");
  expect(toast()).toBe("");
  await press(confirm);
  expect(action.seen).toHaveLength(2);
});

test("a host without actions draws no button on the card, says so, and still links the run", async () => {
  await mount(daemon({ items: [failedAt()] }));
  expect(within(card()).queryAllByRole("button").filter((button) => !button.closest("[data-pager]"))).toEqual([]);
  expect(cardText()).toContain("Actions are off on this host");
  expect(within(card()).getByRole("link", { name: "Open run" })).toBeTruthy();
});

test("a taken requeue says so in the toast and re-reads the fleet at once; a refused one shows the daemon's reason on the card", async () => {
  const asked: string[] = [];
  const state: Daemon = { items: [failedAt(), blocked("DEMO-4", 4, HOUR)], actions: true, asked };
  const taken = fakeFetch({ ok: true, detail: "requeued" });
  state.act = async (url, init) => {
    state.items = [blocked("DEMO-4", 4, HOUR)];
    return taken.fetchImpl(url, init);
  };
  await mount(daemon(state));
  const reads = attentionReads(asked);
  await press(inCard("Requeue"));
  expect(taken.seen.map(({ url, body }) => ({ url, body }))).toEqual([{ url: `${DEMO}/actions/requeue`, body: { ticket: "DEMO-11" } }]);
  expect(screen.getAllByRole("status").find((region) => region.getAttribute("aria-live") === "polite")?.textContent).toBe("Requeued DEMO-11.");
  expect(attentionReads(asked)).toBeGreaterThan(reads);
  expect(card().getAttribute("data-focus")).toBe("DEMO-4");
  cleanup();

  const refused = fakeFetch({ ok: false, detail: "ticket DEMO-11 has a live run" });
  await mount(daemon({ items: [failedAt()], actions: true, act: refused.fetchImpl }));
  await press(inCard("Requeue"));
  expect(card().getAttribute("data-focus")).toBe("DEMO-11");
  expect(within(card()).getByRole("status").textContent).toBe("ticket DEMO-11 has a live run");
  expect(toast()).toBe("");
});

test("Resume opens a reason box on the card whose Send posts the ticket and the note once, then toasts and re-reads the fleet", async () => {
  const asked: string[] = [];
  const action = fakeFetch({ ok: true, detail: "resumed" });
  await mount(daemon({ items: [paused], actions: true, act: action.fetchImpl, asked }));
  const reads = attentionReads(asked);
  await press(inCard("Resume"));
  fireEvent.change(within(card()).getByRole("textbox", { name: "Reason to resume" }), { target: { value: "window is open" } });
  await press(inCard("Send"));
  expect(action.seen.map(({ url, body }) => ({ url, body }))).toEqual([{ url: `${DEMO}/actions/resume`, body: { ticket: "DEMO-6", note: "window is open" } }]);
  expect(toast()).toBe("Resumed DEMO-6.");
  expect(attentionReads(asked)).toBeGreaterThan(reads);
});

test("a host sweep card on a host with no named projects reads the root's actions and posts Run sweep at the root", async () => {
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
  expect(cardText()).not.toContain("Actions are off");
  await press(inCard("Run sweep"));
  expect(action.seen.map(({ url }) => url)).toEqual([`${DEMO}/actions/run-sweep`]);
  expect(toast()).toBe("Started a host sweep on writer-host.");
});

const prUrl = "https://github.com/example/repo/pull/644";
const parkedQuestion = `PR open: ${prUrl}\nchecks failure on the head commit; Fix round made no commit; operator instruction needed:\nI couldn't reproduce the \`unit\` failure.\n\n- **The CI log:** unavailable\n- **The full suite:** passed`;

const fixDeclined = (extra: object = {}) => blocked("DEMO-4", 4, HOUR, {
  park_kind: "fix_declined", pr: { state: null, number: 644, checks: "failure", review: null, threads: null, open_threads: null, title: null }, pr_url: prUrl, question: parkedQuestion, ...extra,
});

test("a declined check fix leads with structured facts and renders the ask without machine prefixes", async () => {
  await mount(daemon({ items: [fixDeclined()] }));
  expect(within(card()).getByRole("heading", { name: "Checks failed on PR #644 and the fix round found nothing to change" })).toBeTruthy();
  expect(within(card()).getByRole("link", { name: "PR #644" }).getAttribute("href")).toBe(prUrl);
  expect(card().querySelector("code")?.textContent).toBe("unit");
  expect(card().querySelector("strong")?.textContent).toBe("The CI log:");
  expect(card().querySelectorAll("li")).toHaveLength(2);
  expect(cardText()).not.toContain("PR open:");
  expect(cardText()).not.toContain("Fix round made no commit");
});

test("a question without a pull request has a generic headline and no PR link", async () => {
  await mount(daemon({ items: [blocked("DEMO-4", 4, HOUR, { park_kind: "question", pr: null, pr_url: null })] }));
  expect(within(card()).getByRole("heading", { name: "The run has a question for you" })).toBeTruthy();
  expect(within(card()).queryByRole("link", { name: /PR/ })).toBeNull();
});

test("the ask displays raw HTML literally and expands from four lines", async () => {
  await mount(daemon({ items: [blocked("DEMO-4", 4, HOUR, { question: "<script>alert(1)</script>" })] }));
  expect(card().querySelector("script")).toBeNull();
  expect(within(card()).getByText("<script>alert(1)</script>")).toBeTruthy();
  const folded = card().querySelector(".line-clamp-4")!;
  expect(folded).toBeTruthy();
  const toggle = within(card()).getByRole("button", { name: "Show all" });
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
  const entry = within(card()).getByRole("list", { name: "Latest progress" }).firstElementChild!;
  expect(entry.querySelector("strong")?.textContent).toBe("Parked");
  expect(entry.querySelector("code")?.textContent).toBe("unit");
  const folded = entry.querySelector(".line-clamp-2")!;
  expect(folded).toBeTruthy();
  const toggle = within(entry as HTMLElement).getByRole("button", { name: "Show all" });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(folded.classList.contains("line-clamp-2")).toBe(false);
  expect(card().querySelector(".line-clamp-4")).toBeTruthy();
});

test.each([
  [{ pr: null }, "The fix round on PR #644 found nothing to change"],
  [{ pr: null, pr_url: null }, "The fix round found nothing to change"],
  [{ park_kind: null }, "The run has a question for you"],
])("the blocked headline falls back using available structured facts (%j)", async (extra, headline) => {
  await mount(daemon({ items: [fixDeclined(extra)] }));
  expect(within(card()).getByRole("heading", { name: headline })).toBeTruthy();
});

test("ask links use safe URLs and unsupported headings and images retain their text", async () => {
  await mount(daemon({ items: [blocked("DEMO-4", 4, HOUR, {
    question: "# Read this\n\n![Important instruction](https://example.com/image.png)\n\n![Reference instruction][image]\n\n[image]: https://example.com/image.png\n\n[Log](https://example.com/log) and [unsafe](javascript:alert%281%29)",
  })] }));
  expect(within(card()).getByText("Read this")).toBeTruthy();
  expect(within(card()).queryByRole("heading", { name: "Read this" })).toBeNull();
  expect(within(card()).getByText("Important instruction")).toBeTruthy();
  expect(within(card()).getByText("Reference instruction")).toBeTruthy();
  expect(card().querySelector("img")).toBeNull();
  const log = within(card()).getByRole("link", { name: "Log" });
  expect(log.getAttribute("href")).toBe("https://example.com/log");
  expect(log.getAttribute("target")).toBe("_blank");
  expect(log.getAttribute("rel")).toBe("noreferrer");
  expect(within(card()).getByText("unsafe").getAttribute("href")).toBe("");
});
