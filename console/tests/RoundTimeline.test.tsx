import { afterEach, expect, test } from "bun:test";
import type { ReactElement } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { GAP_PX, RoundTimeline } from "../src/components/RoundTimeline";
import { buildTimeline, type Segment, type TimelineRun } from "../src/lib/timeline";

const MINUTE = 60_000;
const T = 1_756_900_000_000;

/** A 4%-of-the-bar implement segment, then a review that has run 28m 48s
 *  and is still going. */
const SEGMENTS: Segment[] = [
  { kind: "implement", label: "implement", from: T, to: T + 1.2 * MINUTE, running: false, width: 0.04 },
  {
    kind: "review",
    label: "review 1",
    round: 1,
    from: T + 1.2 * MINUTE,
    to: T + 30 * MINUTE,
    running: true,
    width: 0.96,
  },
];

/** happy-dom lays nothing out, so resolve the component's `calc(A% ± Bpx)`
 *  or `Npx` length against the container width the way the browser would. */
const resolvePx = (css: string, containerPx: number): number => {
  if (/^-?[\d.]+px$/.test(css)) return Number.parseFloat(css);
  const match = /^calc\(([\d.]+)% ([-+]) ([\d.]+)px\)$/.exec(css);
  if (!match) throw new Error(`not a bar length: ${css}`);
  return (Number(match[1]) / 100) * containerPx + (match[2] === "-" ? -1 : 1) * Number(match[3]);
};

/** The live run SEGMENTS came from: 30 minutes in and still reviewing. */
const LIVE = { started_ms: T, ended_ms: null, phase: "reviewing" };

const bar = () => screen.getByRole("list", { name: "Round timeline" });
const segment = (index: number) => bar().querySelectorAll("li")[index] as HTMLElement;

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
  return (Array.from(bar().querySelectorAll("li")) as HTMLElement[]).map((item) => resolvePx(item.style.width, containerPx));
}

/** A run of the given phase changes, `[seconds from start, summary]`, with a 90-minute box. */
const runOf = (changes: [number, string][], ended_ms: number | null, phase: string): TimelineRun => ({
  started_ms: T,
  ended_ms,
  time_box_ms: 90 * MINUTE,
  phase,
  rounds: [],
  events: changes.map(([s, summary]) => ({ at: T + s * 1000, kind: "phase_change", summary })),
});

afterEach(cleanup);

test("a run with one finished implement segment fills the whole bar with it and draws no remaining item", () => {
  const run = runOf([[0, "claimed -> working: KO-1"], [120, "working -> failed: gave up"]], T + 2 * MINUTE, "failed");
  const widths = drawnAt(1000, <RoundTimeline segments={buildTimeline(run, T + 2 * MINUTE)} run={run} now={T + 2 * MINUTE} />);
  expect(Array.from(bar().querySelectorAll("li")).map((item) => item.getAttribute("data-segment"))).toEqual(["implement"]);
  expect(widths[0]).toBeCloseTo(1000, 6);
});

test("7 minutes implementing and a running 3-minute review draw 70% and 30% of the bar after its one gap", () => {
  const run = runOf([[0, "claimed -> working: KO-1"], [420, "working -> reviewing: review"]], null, "reviewing");
  const now = T + 10 * MINUTE;
  const widths = drawnAt(1000, <RoundTimeline segments={buildTimeline(run, now)} run={run} now={now} />);
  expect(widths[0]).toBeCloseTo(0.7 * (1000 - GAP_PX), 6);
  expect(widths[1]).toBeCloseTo(0.3 * (1000 - GAP_PX), 6);
  expect(segment(1).getAttribute("data-running")).toBe("true");
  expect(document.querySelector("[data-timeline-status]")!.textContent).toBe("review · 3m 00s");
});

test("phases of 2 s and 12 s beside 50 minutes keep a 6 px minimum while the bar still sums to its width", () => {
  const run = runOf(
    [[0, "claimed -> working: KO-1"], [3000, "working -> verifying: verify"], [3002, "verifying -> merging: merge"], [3014, "merging -> done: merged"]],
    T + 3014_000,
    "done",
  );
  const widths = drawnAt(1000, <RoundTimeline segments={buildTimeline(run, T + 3014_000)} run={run} now={T + 3014_000} />);
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
  const run = runOf(changes, T + 3020_000, "done");
  const widths = drawnAt(1000, <RoundTimeline segments={buildTimeline(run, T + 3020_000)} run={run} now={T + 3020_000} />);
  expect(widths).toHaveLength(11);
  const tenth = widths.slice(0, 9).reduce((sum, px) => sum + px + GAP_PX, 0) + widths[9]! / 2;
  fireEvent.focusIn(segment(9));
  const tooltip = document.querySelector("[data-segment-tooltip]") as HTMLElement;
  expect(resolvePx(tooltip.style.left, 1000)).toBeCloseTo(tenth, 6);
});

test("each segment is a focusable img naming itself; hovering it floats the long name and duration, leaving hides it", () => {
  render(<RoundTimeline segments={SEGMENTS} run={LIVE} now={T + 30 * MINUTE} />);
  const first = segment(0);
  expect(first.getAttribute("role")).toBe("img");
  expect(first.getAttribute("tabindex")).toBe("0");
  expect(first.getAttribute("aria-label")).toBe("implement 1m 12s");
  expect(first.getAttribute("title")).toBeNull();

  expect(document.querySelector("[data-segment-tooltip]")).toBeNull();
  fireEvent.mouseOver(first);
  const tooltip = document.querySelector("[data-segment-tooltip]")!;
  expect(tooltip.textContent).toBe("Implementation · 1m 12s");
  // The tooltip sits above the bar, centred on the segment.
  const left = (tooltip as HTMLElement).style.left;
  expect(resolvePx(left, 1000)).toBeCloseTo(resolvePx(first.style.width, 1000) / 2, 6);
  fireEvent.mouseOut(first);
  expect(document.querySelector("[data-segment-tooltip]")).toBeNull();

  fireEvent.focusIn(segment(1));
  expect(document.querySelector("[data-segment-tooltip]")!.textContent).toBe("Review · Round 1 · 28m 48s");
  fireEvent.focusOut(segment(1));
  expect(document.querySelector("[data-segment-tooltip]")).toBeNull();
});

test("a finished timeline's status line reads done with the run's whole span, including the minutes no segment covers", () => {
  // The run lived T..T+82m but its first segment opens at T+1m: a
  // segment-to-segment span would read 81m.
  const ended: Segment[] = [
    { kind: "implement", label: "implement", from: T + MINUTE, to: T + 60 * MINUTE, running: false, width: 0.5 },
    { kind: "merge", label: "merge", from: T + 60 * MINUTE, to: T + 81 * MINUTE, running: false, width: 0.5 },
  ];
  render(<RoundTimeline segments={ended} run={{ ...LIVE, ended_ms: T + 82 * MINUTE, phase: "done" }} now={T + 82 * MINUTE} />);
  expect(document.querySelector("[data-timeline-status]")!.textContent).toBe("done · 82m 00s");
});

test("a live run whose last segment closed is parked, not done: the status names its waiting phase and the wait so far", () => {
  const parked: Segment[] = [
    { kind: "implement", label: "implement", from: T, to: T + 30 * MINUTE, running: false, width: 0.5 },
    { kind: "merge", label: "merge", from: T + 30 * MINUTE, to: T + 32 * MINUTE, running: false, width: 0.5 },
  ];
  const run = { ...LIVE, phase: "awaiting_merge_approval" };
  render(<RoundTimeline segments={parked} run={run} now={T + 37 * MINUTE} />);
  expect(document.querySelector("[data-timeline-status]")!.textContent).toBe("awaiting_merge_approval · 5m 00s");
});

test("all kinds have distinct fills, share-sized labels, reason titles and ordered elapsed totals", () => {
  const kinds = ["implement", "wait", "verify", "review", "fix", "parked", "merge", "verify"] as const;
  const lengths = [10, 20, 3, 5, 10, 10, 1, 3];
  let elapsed = 0;
  const segments: Segment[] = kinds.map((kind, i) => {
    const from = T + elapsed * MINUTE;
    elapsed += lengths[i]!;
    return { kind, label: kind === "fix" ? "fix 1" : kind, from, to: T + elapsed * MINUTE,
      width: lengths[i]! / 62, running: false, reason: `reason for ${kind}` };
  });
  render(<RoundTimeline segments={segments} run={{ ...LIVE, ended_ms: T + 62 * MINUTE }} now={T + 62 * MINUTE} />);
  const fills = segments.map((s, i) => {
    const fill = segment(i).querySelector("span")!;
    expect(fill.title).toContain(s.kind === "implement" ? "Implementation" : s.kind === "fix" ? "Rework" : s.kind[0]!.toUpperCase() + s.kind.slice(1));
    expect(fill.title).toContain(`${lengths[i]}m 00s`);
    expect(fill.title).toContain(s.reason!);
    return fill.className.match(/bg-\S+/)![0];
  });
  expect(new Set(fills).size).toBe(7);
  expect(fills[1]).toBe("bg-faint");
  expect(fills[5]).toBe("bg-warn");
  expect(segment(1).textContent).toBe("wait · 20m 00s");
  expect(segment(4).textContent).toBe("rework 1 · 10m 00s");
  expect(segment(6).textContent).toBe("");
  const totals = document.querySelector("[data-timeline-totals]")!;
  expect(totals.textContent).toBe("implement · 10m 00s · wait · 20m 00s · verify · 6m 00s · review · 5m 00s · rework · 10m 00s · parked · 10m 00s · merge · 1m 00s");
  const minutes = [...totals.textContent!.matchAll(/(\d+)m/g)].reduce((sum, match) => sum + Number(match[1]), 0);
  expect(minutes).toBe(62);
});
