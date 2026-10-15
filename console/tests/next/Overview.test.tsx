import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Attention, ShippedRow, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const WRITER = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const shippedRow = ((await Bun.file(new URL("shipped.json", SERVE)).json()) as { rows: ShippedRow[] }).rows[0]!;
const ready = await Bun.file(new URL("run-merge-ready.json", SERVE)).json();

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const NOW = new Date(2026, 9, 15, 12, 0).getTime();

type Item = Attention["items"][number];
const served = (await Bun.file(new URL("attention.json", SERVE)).json()) as Attention;
const park = served.items[0]!;
const blocked: Item = {
  kind: "blocked", level: "attention", ticket: "DEMO-4", ticket_url: null, question: "Which way?", run: 4,
  asked_ms: NOW - (8 * HOUR + 2 * MINUTE), pr_url: null,
};
const failedAt = (run: number, attempt: number, endedMs: number): Item => ({
  kind: "failed", level: "attention", run, ticket: "DEMO-11", ticket_url: null, reason: "verify failed",
  ended_ms: endedMs, attempt, pr_url: null, triage: null,
});
const failed = [failedAt(11, 1, NOW - 3 * HOUR), failedAt(12, 2, NOW - 2 * HOUR)];
const parkOf = (ticket: string, run: number, park_kind: string): Item => ({ ...park, ticket, run, park_kind, asked_ms: NOW - HOUR } as Item);
const ciPark = parkOf("DEMO-8", 8, "ci");
const prPark = parkOf("WRITER-9", 9, "pull_request");

const run = (id: number, ticket: string) => ({ ...working.runs[0]!, id, ticket, heartbeat_age_ms: 30_000 });
const queuedOf = (...tickets: string[]) => tickets.map((ticket) => ({ ...working.queued[0]!, ticket }));
const merged = (id: number, ticket: string, endedMs: number, outcome = "merged"): ShippedRow => ({
  ...shippedRow, id, ticket, ended_ms: endedMs, outcome,
});

interface Daemon {
  items: Item[];
  runs?: Status["runs"];
  queued?: Status["queued"];
  shipped?: ShippedRow[];
}

function fleet(demo: Daemon, writer: Daemon): Fetch {
  const daemon = (project: string, body: Daemon) => ({
    status: { ...working, project, runs: body.runs ?? [], queued: body.queued ?? [] },
    attention: { level: body.items.length ? "attention" : "none", now: NOW, project, items: body.items },
  });
  const polls = peersFetch(DEMO, { [DEMO]: daemon("/srv/dev/demo", demo), [WRITER]: daemon("/srv/dev/writer", writer) });
  return async (url) => {
    if (url === `${WRITER}/runs/9/merge`) return Response.json({ ...ready, run: 9, ticket: "WRITER-9" });
    if (url === `${DEMO}/shipped?limit=50`) return Response.json({ rows: demo.shipped ?? [], next_before: null, limit: 50 });
    if (url === `${WRITER}/shipped?limit=50`) return Response.json({ rows: writer.shipped ?? [], next_before: null, limit: 50 });
    return polls(url);
  };
}

const busy = fleet(
  {
    items: [blocked, ...failed, ciPark],
    runs: [run(3, "DEMO-3"), run(7, "DEMO-7")],
    queued: queuedOf("DEMO-2"),
    shipped: [merged(10, "DEMO-10", NOW - 30 * MINUTE)],
  },
  { items: [prPark] },
);

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

const heading = () => screen.queryByRole("heading", { level: 1 })?.textContent ?? null;
const countLine = () => document.querySelector("[data-count-line]")?.textContent ?? null;
const section = (name: string) => screen.getByRole("region", { name });
const sectionHeader = (name: string) => section(name).querySelector("header")!.textContent;

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("unscoped, the heading counts every host's Needs you entries and the count line every host's work", async () => {
  await mount("#/", busy);
  expect(heading()).toBe("3 need you.");
  expect(countLine()).toBe("2 running · 2 open PRs · 1 queued · 1 merged today");
});

test("a picked project scopes the heading, the count line and the Pull requests count", async () => {
  await mount("#/?project=demo", busy);
  expect(heading()).toBe("2 need you.");
  expect(countLine()).toBe("2 running · 1 open PR · 1 queued · 1 merged today");
  expect(sectionHeader("Pull requests")).toBe("Pull requests1none wait on you");
  cleanup();

  await mount("#/?project=demo", fleet({ items: [blocked] }, { items: [prPark] }));
  expect(heading()).toBe("1 needs you.");
  cleanup();

  await mount("#/?project=demo", fleet({ items: [] }, { items: [prPark] }));
  expect(heading()).toBe("Nothing needs you.");
});

test("the four sections stand in order with their counts and summaries", async () => {
  await mount("#/", busy);
  const labels = screen.getAllByRole("region").map((region) => region.getAttribute("aria-label"));
  expect(labels).toEqual(["Needs you", "Running", "Pull requests", "Merged today"]);
  expect(sectionHeader("Needs you")).toBe("Needs you3oldest waiting 8h 02m");
  expect(sectionHeader("Running")).toBe("Running21 queued");
  expect(sectionHeader("Pull requests")).toBe("Pull requests21 waits on you");
  const clock = new Date(NOW - 30 * MINUTE).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  expect(sectionHeader("Merged today")).toBe(`Merged today1last at ${clock}`);
});

test("a folded section stays folded across a remount, and folds with storage throwing", async () => {
  await mount("#/", busy);
  const label = () => within(section("Running")).getByRole("button", { name: "Running" });
  const body = () => document.getElementById(label().getAttribute("aria-controls")!)!;
  expect(label().getAttribute("aria-expanded")).toBe("true");
  fireEvent.click(label());
  expect(label().getAttribute("aria-expanded")).toBe("false");
  expect(body().hidden).toBe(true);
  cleanup();

  await mount("#/", busy);
  expect(label().getAttribute("aria-expanded")).toBe("false");
  expect(body().hidden).toBe(true);
  cleanup();

  const real = Object.getOwnPropertyDescriptor(globalThis, "localStorage")!;
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get: () => {
      throw new Error("storage disabled");
    },
  });
  try {
    await mount("#/", busy);
    expect(label().getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(label());
    expect(label().getAttribute("aria-expanded")).toBe("false");
    expect(body().hidden).toBe(true);
  } finally {
    Object.defineProperty(globalThis, "localStorage", real);
  }
});

test("empty sections say what the factory is doing, naming the queue and the picked project", async () => {
  const quiet = fleet({ items: [], queued: queuedOf("DEMO-2", "DEMO-12") }, { items: [] });
  await mount("#/", quiet);
  expect(within(section("Running")).getByText("Nothing running. DEMO-2 and DEMO-12 are queued.")).toBeTruthy();
  expect(within(section("Needs you")).getByText("The factory has it from here.")).toBeTruthy();
  expect(within(section("Pull requests")).getByText("No pull requests open.")).toBeTruthy();
  expect(within(section("Merged today")).getByText("Nothing merged yet today.")).toBeTruthy();
  cleanup();

  await mount("#/?project=demo", quiet);
  expect(within(section("Needs you")).getByText("Nothing in demo is waiting on you.")).toBeTruthy();
});

test("the queue sentence names one key, or three and how many more", async () => {
  await mount("#/?project=demo", fleet({ items: [], queued: queuedOf("DEMO-2") }, { items: [] }));
  expect(within(section("Running")).getByText("Nothing running. DEMO-2 is queued.")).toBeTruthy();
  cleanup();

  const five = queuedOf("DEMO-2", "DEMO-3", "DEMO-4", "DEMO-5", "DEMO-6");
  await mount("#/?project=demo", fleet({ items: [], queued: five }, { items: [] }));
  expect(within(section("Running")).getByText("Nothing running. DEMO-2, DEMO-3, DEMO-4 and 2 more are queued.")).toBeTruthy();
});

test("before any scoped host answers there is no heading and no count line", async () => {
  await mount("#/", () => new Promise<Response>(() => {}));
  expect(heading()).toBeNull();
  expect(countLine()).toBeNull();
  expect(screen.queryByText("Nothing needs you.")).toBeNull();
});
