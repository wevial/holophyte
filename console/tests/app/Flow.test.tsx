import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import type { Fetch } from "../../src/lib/poll";
import type { Flow, Status } from "../../src/lib/contract.gen";
import { fakeDeps, peersFetch, settle } from "../harness";

const ORIGIN = "http://writer-host:7710";
const OLDER = "http://writer-host:7711";
const DAY = 24 * 3600 * 1000;
const NO_WAITS = { pending_checks: 0, checks_failing: 0, thread: 0, rounds_used: 0, behind_main: 0, main_red: 0, approval: 0, other: 0 };

const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

function daysBack(count: number, now = Date.now()): number {
  const day = new Date(now - 1);
  day.setHours(0, 0, 0, 0);
  day.setDate(day.getDate() - count);
  return day.getTime();
}

/** A daemon answering each `/flow` ask with one bucket per day or week of the asked span, merges rising bucket by bucket. */
function flowDaemon(asked: URLSearchParams[], older: Flow | null): Fetch {
  const daemon = (project: string) => ({ status: { ...working, host: "writer-host", project }, attention: { level: "none", now: 0, project, items: [] } });
  const polls = peersFetch(ORIGIN, { [ORIGIN]: daemon("/srv/dev/demo"), ...(older ? { [OLDER]: daemon("/srv/dev/older") } : {}) });
  return async (url, init) => {
    if (older && url.startsWith(`${OLDER}/flow?`)) return Response.json(older);
    if (!url.startsWith(`${ORIGIN}/flow?`)) return polls(url, init);
    const query = new URL(url).searchParams;
    asked.push(query);
    const step = query.get("bucket") === "week" ? 7 : 1;
    const now = Number(query.get("until") ?? Date.now());
    const days = Number(query.get("days"));
    const count = Math.ceil(days / step);
    const flow: Flow = {
      now,
      buckets: Array.from({ length: count }, (_, index) => ({
        start_ms: daysBack(Math.min(days - 1, (count - index) * step - 1), now),
        merged: index + 1,
        rounds_median: 1,
        interventions: 1 + (index % 2),
        hours: { working: 2, waiting: 1 + (index % 3), operator: 0 },
      })),
      phases: { working: 1 },
      waits: NO_WAITS,
      reverted: { count: 0, merged: 1 },
    };
    return Response.json(flow);
  };
}

async function flush() {
  for (let round = 0; round < 6; round += 1) await act(settle);
}

async function mount(hash: string, older: Flow | null = null) {
  const asked: URLSearchParams[] = [];
  happyDOM.setURL(`${ORIGIN}/`);
  window.location.hash = hash;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={ORIGIN} pollDeps={fakeDeps(flowDaemon(asked, older)).deps} />
    </QueryClientProvider>,
  );
  await flush();
  return asked;
}

const windowOf = (query: URLSearchParams) => [query.get("days"), query.get("bucket"), query.get("until")];
const openRange = () => fireEvent.click(screen.getByRole("button", { name: "Range" }));
const choices = () => within(screen.getByRole("group", { name: "Range choices" }));

function day(offset: number): string {
  const at = new Date();
  at.setDate(at.getDate() + offset);
  return `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(2, "0")}-${String(at.getDate()).padStart(2, "0")}`;
}

afterEach(() => {
  cleanup();
  window.location.hash = "";
  happyDOM.setURL("about:blank");
});

test("with no range, or a retired weekly one, in the address the menu offers 7, 30 and 90 days with 30 checked and asks for 30 daily buckets", async () => {
  for (const hash of ["#/flow?range=6w", "#/flow"]) {
    const asked = await mount(hash);
    expect(asked.length).toBeGreaterThan(0);
    expect(asked.map(windowOf)).toEqual(asked.map(() => ["30", "day", null]));
    openRange();
    expect(choices().getAllByRole("button").map((choice) => choice.textContent)).toEqual(["Last 7 days", "Last 30 days", "Last 90 days"]);
    expect(choices().getAllByRole("button", { pressed: true }).map((choice) => choice.textContent)).toEqual(["Last 30 days"]);
    cleanup();
  }
});

test("picking Last 90 days asks for 90 days in weekly buckets and checks it in the address", async () => {
  const asked = await mount("#/flow");
  openRange();
  fireEvent.click(choices().getByRole("button", { name: "Last 90 days" }));
  await flush();
  expect(windowOf(asked.at(-1)!)).toEqual(["90", "week", null]);
  expect(new URLSearchParams(window.location.hash.split("?")[1]).get("range")).toBe("90d");
});

test("each trend's change reads against the day before over Last 7 days and against the week before over Last 90 days", async () => {
  for (const [hash, before] of [["#/flow?range=7d", " vs the day before"], ["#/flow?range=90d", " vs the week before"]]) {
    await mount(hash);
    const trends = [...document.querySelectorAll("[data-change]")];
    expect(trends.map((change) => change.getAttribute("data-change"))).toEqual(["merged", "waiting", "rounds", "interventions"]);
    expect(trends.map((change) => change.textContent!.endsWith(before))).toEqual([true, true, true, true]);
    cleanup();
  }
});

test("a custom range asks for its whole days ending with the To day, by day up to a month and by week beyond", async () => {
  const today = new Date();
  const until = String(new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime());
  for (const [from, bucket] of [[day(-10), "day"], [day(-40), "week"]] as const) {
    const asked = await mount("#/flow");
    openRange();
    fireEvent.change(screen.getByLabelText("From"), { target: { value: from } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: day(-1) } });
    await flush();
    const search = new URLSearchParams(window.location.hash.split("?")[1]);
    expect([search.get("from"), search.get("to")]).toEqual([from, day(-1)]);
    expect(windowOf(asked.at(-1)!)).toEqual([bucket === "day" ? "10" : "40", bucket, until]);
    cleanup();
  }
});

test("a bookmarked From and To over 182 days is cleared from the address and the page asks for the default 30 days", async () => {
  const asked = await mount("#/flow?from=2025-01-01&to=2025-07-02");
  expect(asked.length).toBeGreaterThan(0);
  expect(asked.map(windowOf)).toEqual(asked.map(() => ["30", "day", null]));
  expect(new URLSearchParams(window.location.hash.split("?")[1]).get("from")).toBeNull();
  expect(document.querySelector('[data-number="Merged"]')).not.toBeNull();
});

test("a daemon that answers weekly rows under weeks instead of buckets is left out of the numbers", async () => {
  const week = { start_ms: Date.now() - 7 * DAY, merged: 50, rounds_median: 1, interventions: 0, hours: { working: 1, waiting: 0, operator: 0 } };
  await mount("#/flow?range=7d", { now: Date.now(), weeks: [week], phases: {}, waits: NO_WAITS, reverted: { count: 0, merged: 50 } });
  const figure = (label: string) => document.querySelector(`[data-number="${label}"]`)!.textContent;
  expect([figure("Merged"), figure("Reverted")]).toEqual([String(1 + 2 + 3 + 4 + 5 + 6 + 7), "0 of 1"]);
});

test("over Last 90 days the oldest point's range ends the day before the next week opens", async () => {
  await mount("#/flow?range=90d");
  const [oldest, next] = [...document.querySelector("figure[data-chart]")!.querySelectorAll("[data-point]")];
  const dated = (at: Date) => at.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const opens = new Date(daysBack(89));
  const shuts = new Date(daysBack(84));
  expect(oldest!.getAttribute("aria-label")).toStartWith(`${dated(opens)} – ${opens.getMonth() === shuts.getMonth() ? shuts.getDate() : dated(shuts)}:`);
  expect(next!.getAttribute("aria-label")).toStartWith(`${dated(new Date(daysBack(83)))} – `);
});

test("the 30-day Merges chart labels at most every fourth bar and the latest, while every bar keeps its point", async () => {
  await mount("#/flow");
  const merges = document.querySelector('figure[data-chart="Merges"]')!;
  expect(merges.querySelectorAll("[data-point]").length).toBe(30);
  expect([...merges.querySelectorAll("[data-value]")].map((label) => Number(label.getAttribute("data-value")))).toEqual([0, 4, 8, 12, 16, 20, 24, 28, 29]);
});
