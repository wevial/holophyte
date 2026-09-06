import { expect, test } from "bun:test";
import { countsByKind, describe, oldest } from "../src/lib/attention";
import type { Attention, Status } from "../src/lib/types";
import { fixture } from "./harness";

const allKinds = await fixture<{ status: Status; attention: Attention }>("attention_all_kinds.json");
const thresholds = { heartbeat_stale_ms: 180000, strikes: 3 };

test("a stale run reads as a padded no-heartbeat sentence with a one-unit age", () => {
  const item = { kind: "stale_run", level: "attention", run: 91, ticket: "KO-232", phase: "reviewing", heartbeat_age_ms: 421000 };
  const described = describe(item, thresholds);
  expect(described.body).toBe("No heartbeat for 7m 01s while reviewing");
  expect(described.meta).toBe("run #91 · reviewing");
  expect(described.ageMs).toBe(421000);
  expect(described.pill).toBe("stale run");
  expect(described.actions).toEqual(["Kill run", "Requeue"]);
});

test("the time-box clause joins /status.runs on the run id and appears only past the box", () => {
  const item = { kind: "stale_run", level: "attention", run: 91, ticket: "KO-232", phase: "reviewing", heartbeat_age_ms: 421000 };
  const within = describe(item, thresholds, { runs: allKinds.status.runs });
  expect(within.body).toBe("No heartbeat for 7m 01s while reviewing");
  const over = { ...allKinds.status.runs[0]!, elapsed_ms: 1800000 + 754000 };
  expect(describe(item, thresholds, { runs: [over] }).body).toBe(
    "No heartbeat for 7m 01s while reviewing and 12m 34s over its 30m time box",
  );
});

test("newer-daemon fields show only when present", () => {
  const now = 1756900000000;
  const plain = describe({ kind: "blocked", level: "attention", ticket: "KO-240", question: "Which?" }, thresholds, { now });
  expect(plain.meta).toBeNull();
  expect(plain.ageMs).toBeNull();
  const rich = describe(
    { kind: "blocked", level: "attention", ticket: "KO-240", question: "Which?", run: 95, asked_ms: now - 90000 },
    thresholds,
    { now },
  );
  expect(rich.meta).toMatch(/^run #95 · asked at \d\d:\d\d$/);
  expect(rich.ageMs).toBe(90000);
  const failed = describe({ kind: "failed", level: "attention", run: 88, ticket: "KO-229", reason: "x", attempt: 2 }, thresholds);
  expect(failed.meta).toBe("run #88 · strike 2 of 3");
});

test("the fixture's oldest item is the failure that ended two hours ago, and counts are one per kind", () => {
  const { items, now } = allKinds.attention;
  expect(oldest(items, now)).toEqual({ ageMs: 7200000, ticket: "KO-229" });
  expect(countsByKind(items)).toEqual({ all: 4, blocked: 1, stale_run: 1, failed: 1, supervisor: 1 });
  expect(oldest([{ kind: "blocked", level: "attention", ticket: "KO-1", question: "?" }], now)).toBeNull();
});
