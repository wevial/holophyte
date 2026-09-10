import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { App } from "../src/App";
import { BoardWithLedger as Board } from "./ledger";
import { cardLine, cardsOf, columns } from "../src/lib/board";
import type { Fetch } from "../src/lib/poll";
import type { Attention, BoardBody, ShippedBody, Status } from "../src/lib/types";
import { fakeDeps, fixture, hostOf, settle } from "./harness";

const BASE = "http://writer:7710";
const threeDays = await fixture<{ now: number; shipped: ShippedBody }>("shipped_three_days.json");
const { now } = threeDays;
const MIN = 60_000;

/** Three live runs against a 30 m box: at 50 %, 75 % and 110 %, the last
 *  two with strikes. */
const status: Status = {
  target: "/srv/dev/writer",
  project: "/srv/dev/writer",
  host: "writer",
  now,
  supervisor: { state: "live", pid: 1, heartbeat_age_ms: 1000, host: "writer" },
  thresholds: { heartbeat_stale_ms: 180_000, strikes: 3 },
  runs: [
    { id: 91, ticket: "KO-232", phase: "reviewing", heartbeat_age_ms: 4000, elapsed_ms: 15 * MIN, time_box_ms: 30 * MIN, host: "writer" },
    { id: 93, ticket: "KO-241", phase: "working", heartbeat_age_ms: 2000, elapsed_ms: 22.5 * MIN, time_box_ms: 30 * MIN, host: "writer", strikes: 1 },
    { id: 94, ticket: "KO-238", phase: "verifying", heartbeat_age_ms: 9000, elapsed_ms: 33 * MIN, time_box_ms: 30 * MIN, host: "writer", strikes: 2 },
  ],
};

const attention: Attention = {
  level: "attention",
  now,
  items: [{ kind: "blocked", ticket: "KO-240", question: "Which verify command is the contract?", asked_ms: now - (2 * 60 + 14) * MIN, level: "attention" }],
};

const wire = (ticket: string, title: string, rest: Partial<BoardBody["columns"][number]["tickets"][number]> = {}) => ({
  ticket,
  title,
  time_box_ms: 30 * MIN,
  run: null,
  question: null,
  waits_on: [],
  mirrored_ms: now,
  ...rest,
});

/** Every state populated: one needs_spec, two blocked_on_deps, one ready,
 *  one blocked, three in progress. */
const board: BoardBody = {
  now,
  columns: [
    { state: "needs_spec", tickets: [wire("KO-247", "Estimate vs actual chart")] },
    {
      state: "blocked_on_deps",
      tickets: [
        wire("KO-244", "Verify path resolved from README", { waits_on: ["KO-240"] }),
        wire("KO-250", "Board drag to reorder", { waits_on: ["KO-244", "KO-247"] }),
      ],
    },
    { state: "ready", tickets: [wire("KO-242", "Console reads /runs with cursor paging")] },
    { state: "blocked_on_operator", tickets: [wire("KO-240", "Verify command contract", { question: "Which verify command is the contract?" })] },
    {
      state: "in_flight",
      tickets: [
        wire("KO-232", "Split ledger writer from console reader", { run: 91 }),
        wire("KO-241", "Serve /attention with level rollup", { run: 93 }),
        wire("KO-238", "Fixture loader honours HOLOPHYTE_HOME", { run: 94 }),
      ],
    },
  ],
};

const host = hostOf(status, attention, BASE);
const cards = cardsOf(host, board);

afterEach(cleanup);

/** A daemon answering `/board`, `/shipped`, `/status`, `/attention` and `/peers`. */
const daemonFetch: Fetch = async (url) => {
  if (url.endsWith("/board")) return Response.json(board);
  if (url.includes("/shipped")) return Response.json(threeDays.shipped);
  if (url.endsWith("/status")) return Response.json(status);
  if (url.endsWith("/attention")) return Response.json(attention);
  if (url.endsWith("/peers")) return Response.json({ self: "writer:7710", peers: [] });
  return new Response("not found", { status: 404 });
};

test("columns() files every ticket in exactly one of the five columns, in path order, with counts", () => {
  const grouped = columns(cards);
  expect(grouped.map((column) => [column.state, column.label, column.count])).toEqual([
    ["needs_spec", "needs_spec", 1],
    ["blocked_on_deps", "blocked_on_deps", 2],
    ["ready", "ready", 1],
    ["blocked_on_operator", "blocked", 1],
    ["in_flight", "in progress", 3],
  ]);
  const filed = grouped.flatMap((column) => column.cards.map((card) => card.ticket)).sort();
  expect(filed).toEqual(cards.map((card) => card.ticket).sort());
  expect(new Set(filed).size).toBe(cards.length);
});

test("cardLine() reads per state: no criteria, waits on each id, question age, the run's line, nothing when ready", () => {
  const line = (ticket: string) => cardLine(cards.find((card) => card.ticket === ticket)!);
  expect(line("KO-247")).toBe("no acceptance criteria yet");
  expect(line("KO-244")).toBe("waits on KO-240");
  expect(line("KO-250")).toBe("waits on KO-244 · waits on KO-247");
  expect(line("KO-242")).toBeNull();
  expect(line("KO-240")).toBe("question open 2h 14m");
  expect(line("KO-232")).toBe("#91 · 15m 0s / 30m · hb 4s");
});

test("the view renders the five column headers in order with their counts, each ticket under one", async () => {
  render(<Board hosts={[host]} now={now} deps={{ fetch: daemonFetch }} tz="UTC" />);
  await act(settle);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Board");
  expect(document.querySelector("[data-subtitle]")!.textContent).toBe("8 open tickets · left to right is the path to merge");
  const headers = Array.from(document.querySelectorAll("[data-column]")).map((column) => ({
    state: column.getAttribute("data-column"),
    label: column.querySelector("header span + span")!.textContent,
    count: column.querySelector("[data-count]")!.textContent,
    tickets: Array.from(column.querySelectorAll("[data-ticket]")).map((card) => card.getAttribute("data-ticket")),
  }));
  expect(headers).toEqual([
    { state: "needs_spec", label: "needs_spec", count: "1", tickets: ["KO-247"] },
    { state: "blocked_on_deps", label: "blocked_on_deps", count: "2", tickets: ["KO-244", "KO-250"] },
    { state: "ready", label: "ready", count: "1", tickets: ["KO-242"] },
    { state: "blocked_on_operator", label: "blocked", count: "1", tickets: ["KO-240"] },
    { state: "in_flight", label: "in progress", count: "3", tickets: ["KO-232", "KO-241", "KO-238"] },
  ]);
  expect(document.querySelectorAll("[data-ticket]").length).toBe(8);
  expect(screen.getByText("drag to reorder the ready column later")).toBeTruthy();
});

test("an in-progress card shows the phase pill, the strike pill above zero, the bar at 50/75/110 % and the run line", async () => {
  render(<Board hosts={[host]} now={now} deps={{ fetch: daemonFetch }} tz="UTC" />);
  await act(settle);
  const card = (ticket: string) => document.querySelector(`[data-ticket='${ticket}']`) as HTMLElement;
  const bars = ["KO-232", "KO-241", "KO-238"].map((ticket) => within(card(ticket)).getByRole("progressbar"));
  expect(bars.map((bar) => bar.getAttribute("data-tone"))).toEqual(["teal", "amber", "red"]);
  expect(bars.map((bar) => bar.firstElementChild!.className)).toEqual(["bg-accent", "bg-warn", "bg-bad"].map((c) => `block h-full ${c}`));
  expect(bars.map((bar) => (bar.firstElementChild as HTMLElement).style.width)).toEqual(["50%", "75%", "100%"]);
  expect(bars.every((bar) => bar.className.includes("h-[5px]"))).toBe(true);

  expect(card("KO-232").querySelector("[data-phase]")!.textContent).toBe("reviewing");
  expect(card("KO-232").querySelector("[data-strike]")).toBeNull();
  expect(card("KO-241").querySelector("[data-phase]")!.textContent).toBe("implementing");
  expect(card("KO-241").querySelector("[data-strike]")!.textContent).toBe("strike 1/3");
  expect(card("KO-241").querySelector("[data-strike]")!.getAttribute("data-strike")).toBe("amber");
  expect(card("KO-238").querySelector("[data-strike]")!.textContent).toBe("strike 2/3");
  expect(card("KO-238").querySelector("[data-strike]")!.getAttribute("data-strike")).toBe("red");
  expect(card("KO-232").querySelector("[data-line]")!.textContent).toBe("#91 · 15m 0s / 30m · hb 4s");
  expect(within(card("KO-232")).getByText("writer")).toBeTruthy();
});

test("a blocked_on_deps card says what it waits on; a blocked card wears the question pill and its age; a ready card has no line", async () => {
  render(<Board hosts={[host]} now={now} deps={{ fetch: daemonFetch }} tz="UTC" />);
  await act(settle);
  const card = (ticket: string) => document.querySelector(`[data-ticket='${ticket}']`)!;
  expect(card("KO-244").querySelector("[data-line]")!.textContent).toBe("waits on KO-240");
  expect(card("KO-250").querySelector("[data-line]")!.textContent).toBe("waits on KO-244 · waits on KO-247");
  expect(card("KO-247").querySelector("[data-line]")!.textContent).toBe("no acceptance criteria yet");
  const blocked = card("KO-240");
  expect(blocked.querySelector("[data-kind='blocked']")!.textContent).toBe("question");
  expect(blocked.querySelector("[data-line]")!.textContent).toBe("question open 2h 14m");
  expect(card("KO-242").querySelector("[data-line]")).toBeNull();
  expect(card("KO-242").querySelector("[role='progressbar']")).toBeNull();
});

test("every card carries Edit ticket and Mark needs_spec, disabled, with the writes-later tooltip", async () => {
  render(<Board hosts={[host]} now={now} deps={{ fetch: daemonFetch }} tz="UTC" />);
  await act(settle);
  const cards = Array.from(document.querySelectorAll("[data-ticket]"));
  expect(cards.length).toBe(8);
  for (const card of cards) {
    const buttons = Array.from(card.querySelectorAll("[data-actions] button"));
    expect(buttons.map((b) => b.textContent)).toEqual(["Edit ticket", "Mark needs_spec"]);
    for (const button of buttons) {
      expect((button as HTMLButtonElement).disabled).toBe(true);
      expect(button.getAttribute("title")).toBe("Writes arrive later behind a token");
    }
  }
});

test("Shipped today shows only today's rows of a three-day ledger, under its count and median", async () => {
  render(<Board hosts={[host]} now={now} deps={{ fetch: daemonFetch }} tz="UTC" />);
  await act(settle);
  const shipped = screen.getByRole("region", { name: "Shipped today" });
  expect(within(shipped).getByRole("heading", { level: 2 }).textContent).toBe("Shipped today");
  const groups = Array.from(shipped.querySelectorAll("[data-day]")).map((group) => group.getAttribute("data-day"));
  expect(groups).toEqual(["2026-09-05"]);
  expect(shipped.querySelectorAll("[data-row]").length).toBe(3);
  expect(threeDays.shipped.rows.length).toBeGreaterThan(3);
  expect(shipped.querySelector("[data-shipped-subtitle]")!.textContent).toBe("3 merges · median 2 rounds");
});

test("a Shipped today row expands to its run detail the way a Now row does", async () => {
  render(<Board hosts={[host]} now={now} deps={{ fetch: daemonFetch }} tz="UTC" />);
  await act(settle);
  const shipped = screen.getByRole("region", { name: "Shipped today" });
  const rows = Array.from(shipped.querySelectorAll<HTMLElement>("[data-row]"));
  expect(rows.map((row) => row.getAttribute("aria-expanded"))).toEqual(["false", "false", "false"]);
  fireEvent.click(rows[0]!);
  await act(settle);
  expect(rows.map((row) => row.getAttribute("aria-expanded"))).toEqual(["true", "false", "false"]);
  expect(shipped.querySelectorAll("[data-detail]").length).toBe(1);
});

test("clicking Board in the rail opens the view, polling /board from the daemon", async () => {
  const asked: string[] = [];
  const { deps } = fakeDeps(async (url, init) => {
    asked.push(url);
    return daemonFetch(url, init);
  });
  render(<App base={BASE} pollDeps={deps} />);
  await act(settle);
  fireEvent.click(screen.getByRole("button", { name: "Board" }));
  await act(settle);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Board");
  expect(asked).toContain(`${BASE}/board`);
  expect(document.querySelectorAll("[data-column]").length).toBe(5);
});

test("the ledger lives above the view switch: today's rows loaded on Shipped stay under the Board when /shipped later fails", async () => {
  let shippedDown = false;
  const { deps } = fakeDeps(async (url, init) => {
    if (shippedDown && url.includes("/shipped")) return new Response("gone", { status: 503 });
    return daemonFetch(url, init);
  });
  render(<App base={BASE} pollDeps={deps} />);
  await act(settle);
  fireEvent.click(screen.getByRole("button", { name: "Shipped" }));
  await act(settle);
  expect(document.querySelectorAll("[data-row]").length).toBe(threeDays.shipped.rows.length);

  shippedDown = true;
  fireEvent.click(screen.getByRole("button", { name: "Board" }));
  await act(settle);
  const shipped = screen.getByRole("region", { name: "Shipped today" });
  expect(shipped.querySelectorAll("[data-row]").length).toBe(3);
  expect(screen.queryByText("Nothing merged today")).toBeNull();
});
