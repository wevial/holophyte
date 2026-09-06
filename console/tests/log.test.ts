import { expect, test } from "bun:test";
import { formatClock } from "../src/lib/format";
import { logSummary, orderEvents, summarize, timeRange } from "../src/lib/log";
import type { Round, RunEvent } from "../src/lib/types";

const T = 1_756_900_000_000;
const MINUTE = 60_000;

const EVENTS: RunEvent[] = [
  { at: T, kind: "claimed", summary: "claimed KO-232" },
  { at: T + 5 * MINUTE, kind: "phase", summary: "implementing" },
  { at: T + 20 * MINUTE - 4_000, kind: "heartbeat", summary: "heartbeat" },
];

test("logSummary counts the events and names the newest with its age", () => {
  expect(logSummary(EVENTS, T + 20 * MINUTE)).toBe("3 events · last: heartbeat 4s ago");
  expect(logSummary([EVENTS[0]!], T + 7 * MINUTE)).toBe("1 event · last: claimed KO-232 7m ago");
  expect(logSummary([], T)).toBe("No events yet");
});

test("the newest event is found by time even when the wire is out of order", () => {
  const shuffled = [EVENTS[2]!, EVENTS[0]!, EVENTS[1]!];
  expect(logSummary(shuffled, T + 20 * MINUTE)).toBe("3 events · last: heartbeat 4s ago");
  expect(orderEvents(shuffled).map((e) => e.kind)).toEqual(["claimed", "phase", "heartbeat"]);
});

test("timeRange runs first → last on the clock and is empty without events", () => {
  expect(timeRange(EVENTS)).toBe(`${formatClock(T)} → ${formatClock(T + 20 * MINUTE - 4_000)}`);
  expect(timeRange([])).toBe("");
});

/** A merged four-round run's phase changes as the loop writes them
 *  (`FROM -> TO: detail`), one minute apart from `T`. */
const MERGED_FOUR_ROUNDS: RunEvent[] = [
  "claimed -> working: cutting task/ko-305-the-expanded-run and implementing",
  "working -> verifying: ticket verify commands",
  "verifying -> reviewing: round 1 review",
  "reviewing -> addressing: round 1: addressing findings",
  "addressing -> verifying: round 2: verify before review",
  "verifying -> reviewing: round 2 review",
  "reviewing -> addressing: round 2: addressing findings",
  "addressing -> verifying: round 3: verify before review",
  "verifying -> reviewing: round 3 review",
  "reviewing -> addressing: round 3: addressing findings",
  "addressing -> verifying: round 4: verify before review",
  "verifying -> reviewing: round 4 review",
  "reviewing -> merge_gate: pre-merge verify, then the autonomy gate",
  "merge_gate -> merging: --no-ff merge of task/ko-305 into main",
  "merging -> done: merged",
].map((summary, index) => ({ at: T + index * MINUTE, kind: "phase_change", summary }));

const round = (n: number, findings: number): Round => ({
  round: n,
  started_ms: T + (3 * n - 1) * MINUTE,
  ended_ms: T + 3 * n * MINUTE,
  verdict: n === 4 ? "pass" : "changes_requested",
  findings: Array.from({ length: findings }, (_, i) => ({ path: `f${i}.py`, severity: "should", message: "…" })),
});
const ROUNDS: Round[] = [round(1, 2), round(2, 1), round(3, 3), round(4, 0)];

test("a merged four-round run's phase changes summarize in the handoff's words, findings counted from rounds[]", () => {
  expect(MERGED_FOUR_ROUNDS.map((event) => summarize(event, ROUNDS))).toEqual([
    "run started · implementing",
    "implement done · verifying",
    "review round 1 started",
    "review round 1 · 2 findings",
    "fix applied · verifying",
    "review round 2 started",
    "review round 2 · 1 finding",
    "fix applied · verifying",
    "review round 3 started",
    "review round 3 · 3 findings",
    "fix applied · verifying",
    "review round 4 started",
    "review approved · merge gate",
    "merging",
    "merged",
  ]);
});

test("an unmapped transition reads FROM → TO without its detail; other kinds keep their summary", () => {
  expect(summarize({ at: T, kind: "phase_change", summary: "working -> failed: time box exhausted" })).toBe("working → failed");
  expect(summarize({ at: T + 2 * MINUTE, kind: "phase_change", summary: "verifying -> reviewing: terminal adjudication" }, ROUNDS)).toBe(
    "review round 1 started",
  );
  expect(summarize({ at: T, kind: "heartbeat", summary: "heartbeat" }, ROUNDS)).toBe("heartbeat");
  expect(summarize({ at: T, kind: "claimed", summary: "claimed KO-232" })).toBe("claimed KO-232");
});

test("the header's last: is the short line, not the loop's phase text", () => {
  const events = MERGED_FOUR_ROUNDS.slice(0, 4);
  expect(logSummary(events, T + 3 * MINUTE + 4_000, ROUNDS)).toBe("4 events · last: review round 1 · 2 findings 4s ago");
  expect(logSummary(MERGED_FOUR_ROUNDS.slice(0, 1), T + 9_000)).toBe("1 event · last: run started · implementing 9s ago");
});
