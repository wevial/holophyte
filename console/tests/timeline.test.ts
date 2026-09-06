import { expect, test } from "bun:test";
import { boxRemaining, segments, type TimelineRun } from "../src/lib/timeline";

const MINUTE = 60_000;
const T = 1_756_900_000_000;

/** The ticket's fixture: rounds at T+8m..T+12m and T+16m..open, a 30m box. */
const RUN: TimelineRun = {
  started_ms: T,
  ended_ms: null,
  time_box_ms: 30 * MINUTE,
  phase: "reviewing",
  rounds: [
    { started_ms: T + 8 * MINUTE, ended_ms: T + 12 * MINUTE },
    { started_ms: T + 16 * MINUTE, ended_ms: null },
  ],
};

const minutes = (segment: { from: number; to: number }) => (segment.to - segment.from) / MINUTE;
const sum = (values: number[]) => values.reduce((total, value) => total + value, 0);

test("at T+20m: implement 8m, review 4m, fix 4m, review 4m running, widths summing to 20/30", () => {
  const out = segments(RUN, T + 20 * MINUTE);
  expect(out.map((segment) => segment.kind)).toEqual(["implement", "review", "fix", "review"]);
  expect(out.map(minutes)).toEqual([8, 4, 4, 4]);
  expect(out.map((segment) => segment.running)).toEqual([false, false, false, true]);
  expect(out[3]!.label).toBe("reviewing");
  expect(sum(out.map((segment) => segment.width))).toBeCloseTo(20 / 30, 10);
  expect(out[0]!.width).toBeCloseTo(8 / 30, 10);
});

test("at T+40m the widths scale to fill the bar and the box is 10m 00s over", () => {
  const out = segments(RUN, T + 40 * MINUTE);
  expect(out.map(minutes)).toEqual([8, 4, 4, 24]);
  expect(sum(out.map((segment) => segment.width))).toBeCloseTo(1, 10);
  expect(out[0]!.width).toBeCloseTo(8 / 40, 10);
  expect(boxRemaining(RUN, T + 40 * MINUTE)).toBe(-10 * MINUTE);
  expect(boxRemaining(RUN, T + 20 * MINUTE)).toBe(10 * MINUTE);
});

test("a run with no rounds yet is one running implement segment; a phase past its rounds is a fix", () => {
  const fresh = segments({ ...RUN, phase: "working", rounds: [] }, T + 5 * MINUTE);
  expect(fresh.map((segment) => [segment.kind, segment.label, segment.running])).toEqual([["implement", "implementing", true]]);
  const fixing = segments({ ...RUN, phase: "addressing", rounds: [RUN.rounds[0]!] }, T + 15 * MINUTE);
  expect(fixing.map((segment) => [segment.kind, minutes(segment)])).toEqual([
    ["implement", 8],
    ["review", 4],
    ["fix", 3],
  ]);
  expect(fixing[2]!.running).toBe(true);
});
