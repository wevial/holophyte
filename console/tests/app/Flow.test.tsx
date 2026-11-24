import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import type { Fetch } from "../../src/lib/poll";
import type { Flow, Status } from "../../src/lib/contract.gen";
import { fakeDeps, peersFetch, settle } from "../harness";

const ORIGIN = "http://writer-host:7710";
const PEER = "http://writer-host:7711";
const DAY = 24 * 3600 * 1000;
const NO_WAITS = { pending_checks: 0, checks_failing: 0, thread: 0, rounds_used: 0, behind_main: 0, main_red: 0, approval: 0, other: 0 };

const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

function daysBack(count: number, now = Date.now()): number {
  const day = new Date(now - 1);
  return new Date(day.getFullYear(), day.getMonth(), day.getDate() - count).getTime();
}

/** A daemon answering each `/flow` ask with one bucket per day or week of the asked span, merges rising bucket by bucket. */
function flowDaemon(asked: URLSearchParams[], peer: Flow | null, primary: Flow | null = null): Fetch {
  const daemon = (project: string) => ({ status: { ...working, host: "writer-host", project }, attention: { level: "none", now: 0, project, items: [] } });
  const polls = peersFetch(ORIGIN, { [ORIGIN]: daemon("/srv/dev/demo"), ...(peer ? { [PEER]: daemon("/srv/dev/writer") } : {}) });
  return async (url, init) => {
    if (peer && url.startsWith(`${PEER}/flow?`)) return Response.json(peer);
    if (!url.startsWith(`${ORIGIN}/flow?`)) return polls(url, init);
    const query = new URL(url).searchParams;
    asked.push(query);
    if (primary) return Response.json(primary);
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

async function mount(hash: string, peer: Flow | null = null, primary: Flow | null = null) {
  const asked: URLSearchParams[] = [];
  happyDOM.setURL(`${ORIGIN}/`);
  window.location.hash = hash;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={ORIGIN} pollDeps={fakeDeps(flowDaemon(asked, peer, primary)).deps} />
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
  const search = new URLSearchParams(window.location.hash.split("?")[1]);
  expect([search.get("from"), search.get("to")]).toEqual([null, null]);
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

test("the 30-day Merges chart labels bars at least four apart, always the latest, while every bar keeps its point", async () => {
  await mount("#/flow");
  const merges = document.querySelector('figure[data-chart="Merges"]')!;
  expect(merges.querySelectorAll("[data-point]").length).toBe(30);
  expect([...merges.querySelectorAll("[data-value]")].map((label) => Number(label.getAttribute("data-value")))).toEqual([0, 4, 8, 12, 16, 20, 24, 29]);
});

test("each date under a chart is centred on its own bar's point", async () => {
  await mount("#/flow");
  const merges = document.querySelector('figure[data-chart="Merges"]')!;
  const points = [...merges.querySelectorAll<HTMLElement>("[data-point]")];
  const labels = [...merges.querySelectorAll<HTMLElement>("[data-axis]")];
  expect(labels.length).toBeGreaterThan(1);
  for (const label of labels) {
    const point = points[Number(label.getAttribute("data-axis"))]!;
    expect(parseFloat(label.style.left)).toBeCloseTo(parseFloat(point.style.left) + parseFloat(point.style.width) / 2, 6);
    expect(label.classList.contains("-translate-x-1/2")).toBe(true);
  }
});

test("where a clock change skips midnight, a custom range ends at the next midnight and the latest points name only their own days", async () => {
  const zone = process.env.TZ;
  process.env.TZ = "America/Santiago";
  try {
    const asked = await mount("#/flow?from=2026-08-28&to=2026-09-06");
    expect(windowOf(asked.at(-1)!)).toEqual(["10", "day", "1788750000000"]);
    const daily = [...document.querySelector('figure[data-chart="Merges"]')!.querySelectorAll("[data-point]")];
    expect(daily.at(-1)!.getAttribute("aria-label")).toStartWith("Sep 6:");
    cleanup();
    await mount("#/flow?from=2026-07-29&to=2026-09-12");
    const weekly = [...document.querySelector('figure[data-chart="Merges"]')!.querySelectorAll("[data-point]")];
    expect(weekly.at(-1)!.getAttribute("aria-label")).toStartWith("Sep 6 – 12:");
  } finally {
    if (zone == null) delete process.env.TZ;
    else process.env.TZ = zone;
  }
});

test("a host's answer whose days end before today is left out of All projects, yet still shows when its own project is picked", async () => {
  const yesterday = daysBack(0);
  const stale: Flow = {
    now: yesterday,
    buckets: Array.from({ length: 7 }, (_, index) => ({
      start_ms: daysBack(6 - index, yesterday),
      merged: 5,
      rounds_median: 1,
      interventions: 0,
      hours: { working: 1, waiting: 0, operator: 0 },
    })),
    phases: {},
    waits: NO_WAITS,
    reverted: { count: 0, merged: 35 },
  };
  await mount("#/flow?range=7d", stale);
  const figure = (label: string) => document.querySelector(`[data-number="${label}"]`)!.textContent;
  expect([figure("Merged"), figure("Reverted")]).toEqual([String(1 + 2 + 3 + 4 + 5 + 6 + 7), "0 of 1"]);
  expect(screen.getByRole("heading", { level: 1 }).nextElementSibling!.textContent).toStartWith("Seven days, ");
  const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
  expect(rows.map((row) => [row.querySelector("th")!.textContent, row.querySelector("td")!.textContent])).toEqual([["demo", "28"], ["writer", "35"]]);
  cleanup();
  await mount("#/flow?range=7d&project=writer", stale);
  expect([figure("Merged"), figure("Reverted")]).toEqual(["35", "0 of 35"]);
});

const QUIET = { merged: 0, interventions: 0, working: 0, waiting: 0 };
const HIGH_WAIT = { merged: 2, interventions: 1, working: 1, waiting: 3 };
const LOW_WAIT = { merged: 4, interventions: 2, working: 3, waiting: 1 };
const SIXTH_QUIET = [LOW_WAIT, HIGH_WAIT, LOW_WAIT, HIGH_WAIT, HIGH_WAIT, QUIET, LOW_WAIT];

function dailyFlow(specs: (typeof QUIET)[]): Flow {
  const now = Date.now();
  const buckets = specs.map(({ merged, interventions, working, waiting }, index) => ({
    start_ms: daysBack(specs.length - 1 - index, now),
    merged,
    rounds_median: null,
    interventions,
    hours: { working, waiting, operator: 0 },
  }));
  return { now, buckets, phases: {}, waits: NO_WAITS, reverted: null };
}

const shortDay = (startMs: number) => new Date(startMs).toLocaleDateString("en-US", { month: "short", day: "numeric" });

function fillCoversX(path: Element, x: number): boolean {
  const xs = [...(path.getAttribute("d") ?? "").matchAll(/(-?[\d.]+)\s+(-?[\d.]+)/g)].map((match) => Number(match[1]));
  return xs.length > 0 && Math.min(...xs) <= x && x <= Math.max(...xs);
}

async function checkBridgedRatio(days: (typeof QUIET)[], quiet: number[], points: string[], [from, to]: [number, number]) {
  const flow = dailyFlow(days);
  await mount("#/flow?range=7d", null, flow);
  return ["Time spent waiting", "Interventions per merge"].map((title) => {
    const chart = document.querySelector(`figure[data-chart="${title}"]`)!;
    for (const index of quiet) {
      expect(chart.getAttribute("aria-label"), `${title} day ${index}`).toContain(`${shortDay(flow.buckets![index]!.start_ms)} no data`);
    }
    expect([...chart.querySelectorAll("button")].map((point) => point.getAttribute("data-point")), title).toEqual(points);
    expect([...chart.querySelectorAll("[data-dot]")].map((dot) => dot.getAttribute("data-dot")), title).toEqual(points);
    const bridges = [...chart.querySelectorAll("path[data-bridge]")];
    expect(bridges, title).toHaveLength(1);
    const bridge = bridges[0]!;
    expect(bridge.getAttribute("stroke-dasharray"), title).toBeTruthy();
    const match = /^M([\d.]+) ([\d.]+) L([\d.]+) ([\d.]+)$/.exec(bridge.getAttribute("d") ?? "");
    expect(match, title).not.toBeNull();
    const ends = [from, to].flatMap((index) => {
      const dot = chart.querySelector<HTMLElement>(`[data-dot="${index}"]`)!;
      return [parseFloat(dot.style.left), parseFloat(dot.style.top)];
    });
    match!.slice(1).forEach((coord, index) => expect(Math.abs(Number(coord) - ends[index]!), title).toBeLessThanOrEqual(0.01));
    const fills = [...chart.querySelectorAll("svg path:not([data-segment]):not([data-bridge])")];
    return { title, chart, bridge, fills };
  });
}

test("with the sixth of seven days quiet, Interventions reads 0 for that day and draws one unbroken segment across all seven", async () => {
  const flow = dailyFlow(SIXTH_QUIET);
  await mount("#/flow?range=7d", null, flow);
  const chart = document.querySelector('figure[data-chart="Interventions"]')!;
  expect(chart.getAttribute("aria-label")).toContain(`${shortDay(flow.buckets![5]!.start_ms)} 0`);
  expect(chart.getAttribute("aria-label")).not.toContain("no data");
  expect([...chart.querySelectorAll("[data-point]")].map((point) => point.getAttribute("data-point"))).toEqual(["0", "1", "2", "3", "4", "5", "6"]);
  const segments = [...chart.querySelectorAll("path[data-segment]")];
  expect(segments).toHaveLength(1);
  expect(chart.querySelectorAll("path[data-bridge]")).toHaveLength(0);
  const d = segments[0]!.getAttribute("d")!;
  expect(d.match(/M/g)).toHaveLength(1);
  expect(d.match(/C/g)).toHaveLength(6);
});

test("with the sixth of seven days quiet, the ratio charts read it as no data with no point, bridge day five to day seven, and leave the sixth day unfilled", async () => {
  for (const { title, chart, bridge, fills } of await checkBridgedRatio(SIXTH_QUIET, [5], ["0", "1", "2", "3", "4", "6"], [4, 6])) {
    expect(Number(bridge.getAttribute("stroke-width")), title).toBeLessThan(Number(chart.querySelector("path[data-segment]")!.getAttribute("stroke-width")));
    expect(fills.length, title).toBeGreaterThan(0);
    expect(fills.some((fill) => fillCoversX(fill, (5.5 / 7) * 100)), title).toBe(false);
  }
});

test("when a ratio series has only two known days separated by multiple quiet days, one dashed bridge joins them with no point, tooltip or fill on the quiet days", async () => {
  const quietDays = [0, 2, 3, 4, 6];
  const rect = (left: number, width: number) => () => ({ left, top: 0, width, height: 120, right: left + width, bottom: 120, x: left, y: 0, toJSON: () => ({}) });
  for (const { title, chart, bridge, fills } of await checkBridgedRatio([QUIET, HIGH_WAIT, QUIET, QUIET, QUIET, LOW_WAIT, QUIET], quietDays, ["1", "5"], [1, 5])) {
    const tooltip = () => chart.querySelector("[data-tooltip], [role='tooltip']")?.textContent ?? null;
    expect(fills, title).toHaveLength(0);
    const known = [...chart.querySelectorAll<HTMLElement>("button[data-point]")];
    for (const point of known) {
      const index = Number(point.getAttribute("data-point"));
      expect(parseFloat(point.style.left), `${title} slot ${index}`).toBeCloseTo((index / 7) * 100, 6);
      expect(parseFloat(point.style.width), `${title} slot ${index}`).toBeCloseTo(100 / 7, 6);
    }
    fireEvent.mouseEnter(known[0]!);
    expect(tooltip(), `${title} known point`).toBeTruthy();
    fireEvent.mouseLeave(known[0]!);
    expect(tooltip(), `${title} after leave`).toBeNull();
    const plot = known[0]!.parentElement!;
    const svg = chart.querySelector("svg")!;
    chart.getBoundingClientRect = rect(0, 700);
    plot.getBoundingClientRect = rect(0, 700);
    svg.getBoundingClientRect = rect(0, 700);
    bridge.getBoundingClientRect = rect(150, 400);
    for (const index of quietDays) {
      const at = { clientX: (index + 0.5) * 100, clientY: 60 };
      for (const target of [plot, svg, ...(index > 1 && index < 5 ? [bridge] : [])]) {
        fireEvent.mouseEnter(target, at);
        fireEvent.mouseMove(target, at);
        fireEvent.pointerEnter(target, at);
        fireEvent.pointerMove(target, at);
        expect(tooltip(), `${title} quiet day ${index}`).toBeNull();
      }
    }
    for (const target of [chart, ...chart.querySelectorAll("*")]) {
      if (target.matches("[data-point], [data-dot]")) continue;
      fireEvent.mouseEnter(target);
      fireEvent.mouseMove(target, { clientX: 350, clientY: 60 });
      fireEvent.pointerEnter(target);
      fireEvent.focus(target);
      expect(tooltip(), `${title} ${target.tagName}`).toBeNull();
    }
  }
});
