import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { RoundTimeline } from "../src/components/RoundTimeline";
import type { Segment } from "../src/lib/timeline";

const MINUTE = 60_000;
const T = 1_756_900_000_000;

/** A 4%-of-the-bar implement segment, then a review that has run 20 m and
 *  is still going. */
const SEGMENTS: Segment[] = [
  { kind: "implement", label: "implement", from: T, to: T + 1.2 * MINUTE, running: false, width: 0.04 },
  {
    kind: "review",
    label: "review 1",
    round: 1,
    from: T + 1.2 * MINUTE,
    to: T + 21.2 * MINUTE,
    running: true,
    width: 20 / 30,
  },
];

/** happy-dom lays nothing out, so resolve the component's `calc(A% - Bpx)`
 *  width against the fixture's container width the way the browser would. */
const resolvePx = (calc: string, containerPx: number): number => {
  const match = /^calc\(([\d.]+)% - ([\d.]+)px\)$/.exec(calc);
  if (!match) throw new Error(`not a share width: ${calc}`);
  return (Number(match[1]) / 100) * containerPx - Number(match[2]);
};

/** The live run SEGMENTS came from: 21.2 minutes in and still reviewing. */
const LIVE = { started_ms: T, ended_ms: null, phase: "reviewing" };

const bar = () => screen.getByRole("list", { name: "Round timeline" });
const segment = (index: number) => bar().querySelectorAll("li")[index] as HTMLElement;

afterEach(cleanup);

test("the bar keeps its proportional widths, remainder and running pulse; the status line names the running phase", () => {
  render(<RoundTimeline segments={SEGMENTS} run={LIVE} now={T + 21.2 * MINUTE} />);
  const items = Array.from(bar().querySelectorAll("li")) as HTMLElement[];
  expect(items.map((item) => item.getAttribute("data-segment"))).toEqual(["implement", "review", "remaining"]);
  expect(Math.round(resolvePx(items[0]!.style.width, 1000))).toBe(40);
  expect(Math.round(resolvePx(items[1]!.style.width, 1000))).toBe(Math.round((20 / 30) * 1000 - 4));
  expect(items[1]!.getAttribute("data-running")).toBe("true");
  expect(document.querySelector("[data-timeline-status]")!.textContent).toBe("review 1 · 20m 00s");
});

test("each segment is a focusable img naming itself; hovering it floats the long name and duration, leaving hides it", () => {
  render(<RoundTimeline segments={SEGMENTS} run={LIVE} now={T + 21.2 * MINUTE} />);
  const first = segment(0);
  expect(first.getAttribute("role")).toBe("img");
  expect(first.getAttribute("tabindex")).toBe("0");
  expect(first.getAttribute("aria-label")).toBe("implement 1m 12s");
  expect(first.getAttribute("title")).toBeNull();

  expect(document.querySelector("[data-segment-tooltip]")).toBeNull();
  fireEvent.mouseOver(first);
  const tooltip = document.querySelector("[data-segment-tooltip]")!;
  expect(tooltip.textContent).toBe("Implementation · 1m 12s");
  // The tooltip sits above the bar, centred on the segment's share of it.
  expect(tooltip.getAttribute("style")).toContain("left: 2%");
  fireEvent.mouseOut(first);
  expect(document.querySelector("[data-segment-tooltip]")).toBeNull();

  fireEvent.focusIn(segment(1));
  expect(document.querySelector("[data-segment-tooltip]")!.textContent).toBe("Review 1 · 20m 00s");
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
