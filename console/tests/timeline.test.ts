import { expect, test } from "bun:test";
import { boxRemaining, buildTimeline, type TimelineRun } from "../src/lib/timeline";

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
  const out = buildTimeline(RUN, T + 20 * MINUTE);
  expect(out.map((segment) => segment.kind)).toEqual(["implement", "review", "fix", "review"]);
  expect(out.map(minutes)).toEqual([8, 4, 4, 4]);
  expect(out.map((segment) => segment.running)).toEqual([false, false, false, true]);
  expect(out[3]!.label).toBe("reviewing");
  expect(sum(out.map((segment) => segment.width))).toBeCloseTo(20 / 30, 10);
  expect(out[0]!.width).toBeCloseTo(8 / 30, 10);
});

test("at T+40m the widths scale to fill the bar and the box is 10m 00s over", () => {
  const out = buildTimeline(RUN, T + 40 * MINUTE);
  expect(out.map(minutes)).toEqual([8, 4, 4, 24]);
  expect(sum(out.map((segment) => segment.width))).toBeCloseTo(1, 10);
  expect(out[0]!.width).toBeCloseTo(8 / 40, 10);
  expect(boxRemaining(RUN, T + 40 * MINUTE)).toBe(-10 * MINUTE);
  expect(boxRemaining(RUN, T + 20 * MINUTE)).toBe(10 * MINUTE);
});

test("a run with no rounds yet is one running implement segment; a phase past its rounds is a fix", () => {
  const fresh = buildTimeline({ ...RUN, phase: "working", rounds: [] }, T + 5 * MINUTE);
  expect(fresh.map((segment) => [segment.kind, segment.label, segment.running])).toEqual([["implement", "implementing", true]]);
  const fixing = buildTimeline({ ...RUN, phase: "addressing", rounds: [RUN.rounds[0]!] }, T + 15 * MINUTE);
  expect(fixing.map((segment) => [segment.kind, minutes(segment)])).toEqual([
    ["implement", 8],
    ["review", 4],
    ["fix", 3],
  ]);
  expect(fixing[2]!.running).toBe(true);
});

/** Run 125 (KO-262) on the writer host: its phase changes, seconds from start. */
const CHANGES: [number, string][] = [
  [0, "claimed -> working: KO-262"],
  [185, "working -> verifying: round 1: verify before review"],
  [253, "verifying -> reviewing: round 1 review"],
  [394, "reviewing -> addressing: round 1: 2 findings to address"],
  [873, "addressing -> verifying: round 2: verify before review"],
  [942, "verifying -> reviewing: round 2 review"],
  [1135, "reviewing -> addressing: round 2: 1 finding to address"],
  [1466, "addressing -> verifying: round 3: verify before review"],
  [1536, "verifying -> reviewing: round 3 review"],
  [1670, "reviewing -> addressing: round 3: 1 finding to address"],
  [1923, "addressing -> verifying: round 4: verify before review"],
  [1993, "verifying -> reviewing: round 4 review"],
  [2124, "reviewing -> merge_gate: approved"],
  [2195, "merge_gate -> merging: fast-forward to main"],
  [2196, "merging -> done: merged"],
];

const events = (changes: [number, string][]) =>
  changes.map(([s, summary]) => ({ at: T + s * 1000, kind: "phase_change", summary }));
const seconds = (segment: { from: number; to: number }) => (segment.to - segment.from) / 1000;

test("a merged four-round run's phase changes yield implement, 4× (verify, review N, fix N), verify, merge", () => {
  const run: TimelineRun = { ...RUN, phase: "done", ended_ms: T + 2196_000, rounds: [], events: events(CHANGES) };
  const out = buildTimeline(run, T + 3 * 3600_000);
  expect(out.map((segment) => segment.label)).toEqual([
    "implement",
    "verify", "review 1", "fix 1",
    "verify", "review 2", "fix 2",
    "verify", "review 3", "fix 3",
    "verify", "review 4",
    "verify", "merge",
  ]);
  expect(out.map((segment) => segment.kind)).toEqual([
    "implement",
    "verify", "review", "fix",
    "verify", "review", "fix",
    "verify", "review", "fix",
    "verify", "review",
    "verify", "merge",
  ]);
  expect(out.map(seconds)).toEqual([185, 68, 141, 479, 69, 193, 331, 70, 134, 253, 70, 131, 71, 1]);
  expect(out.every((segment) => !segment.running)).toBe(true);
  // 2196 s is past the 30 m box, so widths are each duration over the run.
  expect(out[2]!.width).toBeCloseTo(141 / 2196, 10);
  expect(sum(out.map((segment) => segment.width))).toBeCloseTo(1, 10);
});

test("a live run mid-review is implement, verify and a running review 1 of 200 s that pulses", () => {
  const run: TimelineRun = { ...RUN, rounds: [], events: events(CHANGES.slice(0, 3)) };
  const out = buildTimeline(run, T + 453_000);
  expect(out.map((segment) => [segment.label, seconds(segment), segment.running])).toEqual([
    ["implement", 185, false],
    ["verify", 68, false],
    ["review 1", 200, true],
  ]);
  expect(out[2]!.width).toBeCloseTo(200 / (30 * 60), 10);
});

test("a run with no phase_change events falls back to the rounds derivation", () => {
  const now = T + 20 * MINUTE;
  const noise = [{ at: T, kind: "claimed", summary: "claimed KO-232" }];
  expect(buildTimeline({ ...RUN, events: noise }, now)).toEqual(buildTimeline(RUN, now));
  expect(buildTimeline({ ...RUN, events: [] }, now).map((segment) => segment.kind)).toEqual(["implement", "review", "fix", "review"]);
});
