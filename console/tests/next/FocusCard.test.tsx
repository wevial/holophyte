import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { Attention, RunDetail, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
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
  asked_ms: NOW - askedAgo, pr_url: null, ...extra,
} as Item);
const code = { choice: "code", confidence: 0.81, backend: "claude", model: "triage-model", requeued: false };

interface Daemon {
  items: Item[];
  runs?: Status["runs"];
  details?: Record<number, RunDetail>;
}

/** One project daemon whose bodies the test may change between polls. */
function daemon(state: Daemon): Fetch {
  return async (url) => {
    const merge = url.match(/\/runs\/(\d+)\/merge$/);
    if (merge) return Response.json({ ...ready, run: Number(merge[1]) });
    const match = url.match(/\/runs\/(\d+)$/);
    if (match) {
      const body = state.details?.[Number(match[1])];
      return body ? Response.json(body) : new Response("no such run", { status: 404 });
    }
    return peersFetch(DEMO, {
      [DEMO]: {
        status: { ...working, thresholds: { ...working.thresholds, strikes: 3 }, now: NOW, project: "/srv/dev/demo", runs: state.runs ?? [], queued: [] },
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

afterEach(() => {
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
  expect([...lines].map((line) => line.textContent)).toEqual([
    "16:02verify failed: 2 tests red",
    "15:55working -> verifying",
    "15:40round 1 changes requested",
  ]);
  expect(cardText()).toContain("No pull request yet");
  cleanup();

  const park = {
    kind: "pr_open", level: "attention", ticket: "DEMO-9", ticket_url: null, title: "Link each run to its pull request", run: 9, park_kind: "pull_request", asked_ms: NOW - HOUR,
    reason: "PR open", pr_url: "https://github.com/example/demo/pull/42",
    pr: { number: 42, checks: "success", review: "approved", threads: 0, open_threads: 0, title: "Link each run" },
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
