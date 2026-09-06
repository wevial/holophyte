import { expect, test } from "bun:test";
import { boxFill, boxRatio, deltaLabel, deltaTone, groupByDay, medianRounds, minutesLabel, tagRows, withinDays } from "../src/lib/shipped";
import type { ShippedBody } from "../src/lib/types";
import { fixture } from "./harness";

const threeDays = await fixture<{ now: number; shipped: ShippedBody }>("shipped_three_days.json");
const ROWS = tagRows({ base: "http://writer:7710", project: "/srv/dev/writer" }, threeDays.shipped.rows);

test("rows spanning three days group newest-first under Today, Yesterday and the bare date, in the daemon's day", () => {
  const groups = groupByDay(ROWS, threeDays.now, "UTC");
  expect(groups.map((group) => group.label)).toEqual(["Today · Sat Sep 5", "Yesterday · Fri Sep 4", "Thu Sep 3"]);
  expect(groups.map((group) => group.daysAgo)).toEqual([0, 1, 2]);
  expect(groups.map((group) => group.rows.map((row) => row.id))).toEqual([[236, 235, 234], [228, 227], [224, 223]]);
  expect(withinDays(groups, 1).map((group) => group.key)).toEqual(["2026-09-05"]);
});

test("the day is the viewer's local one: 23:30 UTC on the 4th is the 5th in Tokyo", () => {
  const late = { ...ROWS[0]!, ended_ms: Date.UTC(2026, 8, 4, 23, 30) };
  expect(groupByDay([late], threeDays.now, "UTC")[0]!.label).toBe("Yesterday · Fri Sep 4");
  expect(groupByDay([late], threeDays.now, "Asia/Tokyo")[0]!.label).toBe("Today · Sat Sep 5");
});

test("the bar is ok at or under 80 %, warn above 80 % to 100 %, over past it; the delta signs and tones follow", () => {
  expect([24, 25, 30, 38].map((actual) => boxRatio(actual, 30))).toEqual(["ok", "warn", "warn", "over"]);
  expect([24, 25, 30, 38].map((actual) => deltaLabel(actual, 30))).toEqual(["−6m", "−5m", "0m", "+8m"]);
  expect([24, 25, 30, 38].map((actual) => deltaTone(actual, 30))).toEqual(["ok", "ok", "muted", "over"]);
  expect(boxFill(38, 30)).toBe(1);
  expect(boxFill(24, 30)).toBeCloseTo(0.8, 9);
});

test("no box: the bar is empty, the minutes read Nm / — and there is no delta", () => {
  expect(boxRatio(15, null)).toBeNull();
  expect(boxRatio(15, 0)).toBeNull();
  expect(boxFill(15, null)).toBe(0);
  expect(minutesLabel(15.4, null)).toBe("15m / —");
  expect(minutesLabel(17.6, 30)).toBe("18m / 30m");
  expect(deltaLabel(15, null)).toBe("");
});

test("medianRounds is the middle value, the mean of the middle two when even, null with nothing", () => {
  expect(medianRounds(threeDays.shipped.rows)).toBe(2);
  expect(medianRounds([{ rounds: 1 }, { rounds: 3 }])).toBe(2);
  expect(medianRounds([{ rounds: 1 }, { rounds: 1 }, { rounds: 3 }, { rounds: 5 }])).toBe(2);
  expect(medianRounds([])).toBeNull();
});

test("rows from two daemons each carry the name of that daemon's /status project", () => {
  const wire = threeDays.shipped.rows.map((row) => ({ ...row, host: "writer-1" }));
  const merged = [
    ...tagRows({ base: "http://writer:7710", project: "/srv/dev/holophyte" }, wire.slice(0, 2)),
    ...tagRows({ base: "http://writer:7711", project: "/srv/dev/croton-mcp/" }, wire.slice(2, 4)),
  ];
  expect(merged.map((row) => row.project)).toEqual(["holophyte", "holophyte", "croton-mcp", "croton-mcp"]);
  expect(merged.map((row) => row.daemon)).toEqual(["http://writer:7710", "http://writer:7710", "http://writer:7711", "http://writer:7711"]);
  expect(merged.map((row) => row.id)).toEqual(wire.slice(0, 4).map((row) => row.id));
  expect(tagRows({ base: "http://writer:7712", project: null }, wire.slice(0, 1))[0]!.project).toBe("");
});
