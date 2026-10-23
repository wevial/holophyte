import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import type { ReactElement } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { RunAsks, RunDetail } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { GAP_PX } from "../../src/lib/timeline";
import { PhaseBar } from "../../src/next/components/PhaseBar";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, run, SERVE } from "./needsFleet";

const servedRun = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const [servedRound] = servedRun.rounds;
const at = (hour: number, minute: number) => new Date(2026, 9, 15, hour, minute).getTime();
const change = (time: number, summary: string) => ({ at: time, kind: "phase_change", summary });
const noAsks: RunAsks = { run: 12, ticket: "DEMO-12", pr_url: null, asks: [] };

const mergedDetail: RunDetail = {
  ...servedRun,
  run: {
    ...servedRun.run, id: 12, ticket: "DEMO-12", title: "Sweep the leases", phase: "done", outcome: "merged",
    started_ms: at(16, 36), ended_ms: at(17, 2), heartbeat_age_ms: null,
  },
  events: [
    change(at(16, 36), "claimed -> working"),
    change(at(16, 50), "working -> verifying"),
    change(at(16, 52), "verifying -> reviewing"),
    change(at(17, 0), "reviewing -> merging"),
  ],
  rounds: [{ ...servedRound!, round: 1, started_ms: at(16, 52), ended_ms: at(16, 59), verdict: "pass", findings: [] }],
  steer_notes: [],
};

const liveDetail: RunDetail = {
  ...servedRun,
  run: { ...servedRun.run, id: 12, ticket: "DEMO-12", title: "Sweep the leases", phase: "reviewing", started_ms: at(16, 36) },
  events: [change(at(16, 36), "claimed -> working"), change(at(16, 45), "working -> reviewing")],
  rounds: [{ ...servedRound!, round: 1, started_ms: at(16, 45), ended_ms: null, verdict: "", findings: [] }],
  steer_notes: [],
};

/** DEMO answering `/runs/12` with `detail`, run 12 listed live in `/status` when `live`. */
function runFetch(detail: RunDetail, live: boolean): Fetch {
  const runs = live ? [{ ...run(12, "DEMO-12"), phase: "reviewing", round: 1, max_rounds: 3 }] : [];
  const polls = fleet({ items: [], runs }, { items: [] });
  return async (url) => {
    if (url === `${DEMO}/runs/12`) return Response.json(detail);
    if (url === `${DEMO}/runs/12/asks`) return Response.json(noAsks);
    return polls(url);
  };
}

async function mount(fetch: Fetch, now: number) {
  window.location.hash = "#/runs/demo/12";
  const fake = fakeDeps(fetch);
  fake.clock.now = now;
  const queries = consoleQueryClient();
  render(
    <QueryClientProvider client={queries}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
  return { queries, clock: fake.clock };
}

const progress = () => screen.getByRole("region", { name: "Progress" });
const bar = () => screen.getByRole("list", { name: "Phase timeline" });
const segments = () => Array.from(bar().querySelectorAll("li")) as HTMLElement[];
const names = () => segments().map((item) => item.getAttribute("aria-label"));
const tooltip = () => document.querySelector("[data-segment-tooltip]");

/** happy-dom lays nothing out, so resolve the bar's `calc(A% ± Bpx)` or
 *  `Npx` length against the container width the way the browser would. */
const resolvePx = (css: string, containerPx: number): number => {
  if (/^-?[\d.]+px$/.test(css)) return Number.parseFloat(css);
  const match = /^calc\(([\d.]+)% ([-+]) ([\d.]+)px\)$/.exec(css);
  if (!match) throw new Error(`not a bar length: ${css}`);
  return (Number(match[1]) / 100) * containerPx + (match[2] === "-" ? -1 : 1) * Number(match[3]);
};

/** Renders with every element measuring `containerPx` wide, as the bar
 *  would in a container that width, and returns each item's resolved px. */
function drawnAt(containerPx: number, element: ReactElement): number[] {
  const saved = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => containerPx });
  try {
    render(element);
  } finally {
    if (saved) Object.defineProperty(HTMLElement.prototype, "clientWidth", saved);
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth;
  }
  return segments().map((item) => resolvePx(item.style.width, containerPx));
}

const T = at(10, 0);

/** A run of the given phase changes, `[seconds from start, summary]`, ended at `endedS` or live. */
const runOf = (changes: [number, string][], endedS: number | null): RunDetail => ({
  ...servedRun,
  run: { ...servedRun.run, started_ms: T, ended_ms: endedS == null ? null : T + endedS * 1000, phase: endedS == null ? "reviewing" : "done" },
  rounds: [],
  events: changes.map(([s, summary]) => change(T + s * 1000, summary)),
});

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => {
  happyDOM.setURL("about:blank");
  if (zone == null) delete process.env.TZ;
  else process.env.TZ = zone;
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("a merged run's Progress opens on its implement, verify, review and merge segments, none running, over the totals", async () => {
  await mount(runFetch(mergedDetail, false), at(17, 30));
  expect(progress().contains(bar())).toBe(true);
  expect(bar().compareDocumentPosition(progress().querySelector("ol[aria-live]")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(segments().map((item) => item.getAttribute("data-segment"))).toEqual(["implement", "verify", "review", "merge"]);
  expect(names()).toEqual(["implement 14m 00s", "verify 2m 00s", "Review · Round 1 8m 00s", "merge 2m 00s"]);
  expect(segments().filter((item) => item.hasAttribute("data-running"))).toEqual([]);
  expect(progress().querySelector("[data-phase-totals]")!.textContent).toBe(
    "implement · 14m 00s · verify · 2m 00s · review · 8m 00s · merge · 2m 00s",
  );
});

test("a live run's running review grows with the fleet clock across polls while the implement segment holds", async () => {
  const { queries, clock } = await mount(runFetch(liveDetail, true), at(16, 50));
  expect(segments().at(-1)!.getAttribute("data-running")).toBe("true");
  expect(names()).toEqual(["implement 9m 00s", "Review · Round 1 5m 00s"]);
  clock.now = at(16, 53);
  await act(async () => {
    await queries.refetchQueries();
  });
  for (let round = 0; round < 3; round += 1) await act(settle);
  expect(names()).toEqual(["implement 9m 00s", "Review · Round 1 8m 00s"]);
  expect(segments().at(-1)!.getAttribute("data-running")).toBe("true");
});

test("focusing the live review segment shows its tooltip with its round, duration and opening event; blurring removes it", async () => {
  await mount(runFetch(liveDetail, true), at(16, 50));
  expect(tooltip()).toBeNull();
  fireEvent.focusIn(segments()[1]!);
  expect(tooltip()!.textContent).toBe("Review · Round 1 · 5m 00s · working -> reviewing");
  fireEvent.focusOut(segments()[1]!);
  expect(tooltip()).toBeNull();
});

test("7 minutes implementing and a running 3-minute review draw 70% and 30% of the bar after its one gap", () => {
  const detail = runOf([[0, "claimed -> working: KO-1"], [420, "working -> reviewing: review"]], null);
  const widths = drawnAt(1000, <PhaseBar detail={detail} now={T + 600_000} />);
  expect(widths[0]).toBeCloseTo(0.7 * (1000 - GAP_PX), 6);
  expect(widths[1]).toBeCloseTo(0.3 * (1000 - GAP_PX), 6);
});

test("implement, verify, review, rework and parked segments each pair their fill with its own ink", () => {
  const detail = runOf(
    [[0, "claimed -> working: KO-1"], [600, "working -> verifying: verify"], [900, "verifying -> reviewing: review"],
      [1200, "reviewing -> addressing: fix"], [1500, "addressing -> blocked_on_operator: parked"], [1800, "blocked_on_operator -> done: merged"]],
    1800,
  );
  drawnAt(1000, <PhaseBar detail={detail} now={T + 1800_000} />);
  expect(segments().map((item) => [item.getAttribute("data-segment"), [...item.querySelector("span")!.classList].filter((name) => /^(bg|text)-(?!center|\[)/.test(name))])).toEqual([
    ["implement", ["bg-act", "text-act-ink"]],
    ["verify", ["bg-live", "text-green-on"]],
    ["review", ["bg-wait", "text-violet-on"]],
    ["fix", ["bg-ask", "text-gold-on"]],
    ["parked", ["bg-fact-warn", "text-orange-on"]],
  ]);
});

test("phases of 2 s and 12 s beside 50 minutes keep a 6 px minimum while the bar still sums to its width", () => {
  const detail = runOf(
    [[0, "claimed -> working: KO-1"], [3000, "working -> verifying: verify"], [3002, "verifying -> merging: merge"], [3014, "merging -> done: merged"]],
    3014,
  );
  const widths = drawnAt(1000, <PhaseBar detail={detail} now={T + 3014_000} />);
  expect(widths).toHaveLength(3);
  expect(widths[1]).toBeGreaterThanOrEqual(6);
  expect(widths[2]).toBeGreaterThanOrEqual(6);
  expect(widths.reduce((sum, px) => sum + px, 0) + (widths.length - 1) * GAP_PX).toBeCloseTo(1000, 6);
});

test("a widened short segment's tooltip centres on where it draws, past the widened segments before it", () => {
  const changes: [number, string][] = [[0, "claimed -> verifying: verify"]];
  for (let index = 1; index < 10; index++) {
    changes.push([2 * index, index % 2 ? "verifying -> reviewing: review" : "reviewing -> verifying: verify"]);
  }
  changes.push([20, "reviewing -> working: rework"], [20 + 3000, "working -> done: merged"]);
  const widths = drawnAt(1000, <PhaseBar detail={runOf(changes, 3020)} now={T + 3020_000} />);
  expect(widths).toHaveLength(11);
  const tenth = widths.slice(0, 9).reduce((sum, px) => sum + px + GAP_PX, 0) + widths[9]! / 2;
  fireEvent.focusIn(segments()[9]!);
  expect(resolvePx((tooltip() as HTMLElement).style.left, 1000)).toBeCloseTo(tenth, 6);
});

test("40 equal segments in a 320 px bar too narrow for their minimums still fit it, the last tooltip centred on the last segment", () => {
  const changes: [number, string][] = [[0, "claimed -> verifying: verify"]];
  for (let index = 1; index < 40; index++) {
    changes.push([60 * index, index % 2 ? "verifying -> reviewing: review" : "reviewing -> verifying: verify"]);
  }
  changes.push([2400, "reviewing -> done: merged"]);
  const widths = drawnAt(320, <PhaseBar detail={runOf(changes, 2400)} now={T + 2400_000} />);
  expect(widths).toHaveLength(40);
  const gap = Number.parseFloat(bar().style.gap);
  expect(widths.every((px) => px > 0)).toBe(true);
  expect(widths.reduce((sum, px) => sum + px, 0) + 39 * gap).toBeCloseTo(320, 3);
  fireEvent.focusIn(segments()[39]!);
  expect(resolvePx((tooltip() as HTMLElement).style.left, 320)).toBeCloseTo(320 - widths[39]! / 2, 3);
});
