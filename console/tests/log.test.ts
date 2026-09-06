import { expect, test } from "bun:test";
import { formatClock } from "../src/lib/format";
import { logSummary, orderEvents, timeRange } from "../src/lib/log";
import type { RunEvent } from "../src/lib/types";

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
