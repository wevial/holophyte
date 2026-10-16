import { expect, test } from "bun:test";
import { countsByKind, describe, needsYouEntries, type ReadinessMap } from "../src/lib/attention";
import { runKey } from "../src/lib/runs";
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
  expect(described.actions).toEqual(["Abort", "Requeue"]);
});

test("the time-box clause joins /status.runs on the run id and appears only past the box", () => {
  const item = { kind: "stale_run", level: "attention", run: 91, ticket: "KO-232", phase: "reviewing", heartbeat_age_ms: 421000 };
  const within = describe(item, thresholds, { runs: allKinds.status.runs });
  expect(within.body).toBe("No heartbeat for 7m 01s while reviewing");
  const over = { ...allKinds.status.runs[0]!, working_ms: 1800000 + 754000 };
  expect(describe(item, thresholds, { runs: [over] }).body).toBe(
    "No heartbeat for 7m 01s while reviewing and 12m 34s over its 30m time box",
  );
});

test("the time-box clause reads the agent clock when the daemon serves it", () => {
  const item = { kind: "stale_run", level: "attention", run: 91, ticket: "KO-232", phase: "reviewing", heartbeat_age_ms: 421000 };
  const base = { ...allKinds.status.runs[0]!, time_box_ms: 1800000, working_ms: 2100000 };
  expect(describe(item, thresholds, { runs: [{ ...base, agent_ms: 1200000 }] }).body).toBe("No heartbeat for 7m 01s while reviewing");
  expect(describe(item, thresholds, { runs: [{ ...base, agent_ms: 1800000 + 754000 }] }).body).toBe(
    "No heartbeat for 7m 01s while reviewing and 12m 34s over its 30m time box",
  );
});

test("a stuck wait names its dependency, the dependency's state and reason, and the queue behind it", () => {
  const base = { kind: "stuck_wait", level: "attention", ticket: "NAT-3", ticket_url: null, title: null, dependency: "NAT-2" };
  const spec = { ...base, dependency_state: "needs_spec", reason: "critic: stale — already shipped by NAT-1", behind: 4 };
  const gone = { ...base, dependency_state: "abandoned", reason: null, behind: 0 };
  const first = describe(spec, thresholds);
  expect(first.pill).toBe("stuck");
  expect(first.actions).toEqual([]);
  expect(first.body).toBe("Waits on NAT-2, which needs a spec: critic: stale — already shipped by NAT-1");
  expect(first.meta).toBe("4 queued behind");
  const second = describe(gone, thresholds);
  expect(second.body).toBe("Waits on NAT-2, which was abandoned");
  expect(second.meta).toBeNull();
  expect(second.actions).toEqual([]);
  expect(countsByKind([spec, gone]).stuck_wait).toBe(2);
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

test("the fixture's counts are one per kind", () => {
  expect(countsByKind(allKinds.attention.items)).toEqual({ all: 4, blocked: 1, pr_open: 0, paused: 0, stale_run: 1, failed: 1, stuck_wait: 0, supervisor: 1, unreachable: 0 });
});

const HOUR = 3_600_000;
const NOW = 100 * HOUR;
const DAEMON = "writer-host:7710";
const needsYouItems = [
  { kind: "pr_open", level: "attention", daemon: DAEMON, ticket: "DEMO-8", run: 8, park_kind: "ci", asked_ms: NOW - 4 * HOUR },
  { kind: "blocked", level: "attention", daemon: DAEMON, ticket: "DEMO-4", run: 4, asked_ms: NOW - 2 * HOUR },
  { kind: "failed", level: "attention", daemon: DAEMON, ticket: "DEMO-11", run: 12, ended_ms: NOW - HOUR },
  { kind: "stale_run", level: "attention", daemon: DAEMON, ticket: "DEMO-3", run: 3, heartbeat_age_ms: 20 * 60_000 },
  { kind: "pr_open", level: "attention", daemon: DAEMON, ticket: "DEMO-9", run: 9, park_kind: "pull_request", asked_ms: NOW - 90 * 60_000 },
  { kind: "failed", level: "attention", daemon: DAEMON, ticket: "DEMO-11", run: 11, ended_ms: NOW - 5 * HOUR },
  { kind: "unreachable", level: "critical", daemon: "writer-host:7711", asked_ms: NOW - 3 * HOUR },
];
const shape = (map: ReadinessMap) =>
  needsYouEntries(needsYouItems, map, NOW).map((entry) => ({
    kind: entry.item.kind,
    ticket: entry.item.ticket ?? null,
    run: entry.item.run ?? null,
    attempts: entry.attempts?.map((attempt) => attempt.run),
    readyToMerge: entry.readyToMerge,
  }));

test("needs you keeps every item but a pull request park, one entry per failed ticket, longest-waited first", () => {
  expect(shape(new Map())).toEqual([
    { kind: "unreachable", ticket: null, run: null, attempts: undefined, readyToMerge: false },
    { kind: "blocked", ticket: "DEMO-4", run: 4, attempts: undefined, readyToMerge: false },
    { kind: "failed", ticket: "DEMO-11", run: 12, attempts: [11, 12], readyToMerge: false },
    { kind: "stale_run", ticket: "DEMO-3", run: 3, attempts: undefined, readyToMerge: false },
  ]);
});

test("needs you keeps a pull_request park only while its run is ready to merge, and never a ci park", () => {
  const ready = new Map([[runKey(DAEMON, 8), { ready: true }], [runKey(DAEMON, 9), { ready: true }]]);
  expect(shape(ready).map((entry) => [entry.ticket, entry.readyToMerge])).toEqual([
    [null, false],
    ["DEMO-4", false],
    ["DEMO-9", true],
    ["DEMO-11", false],
    ["DEMO-3", false],
  ]);
  const notReady = new Map([[runKey(DAEMON, 8), { ready: true }], [runKey(DAEMON, 9), { ready: false }]]);
  expect(shape(notReady).map((entry) => entry.ticket)).toEqual([null, "DEMO-4", "DEMO-11", "DEMO-3"]);
});


test("needs you offers bypass only for a pull_request park with review_bypassable readiness", () => {
  const readiness = new Map([8, 9].map((run) => [runKey(DAEMON, run), { ready: false, reason: "review_bypassable" }]));
  const entries = needsYouEntries(needsYouItems, readiness, NOW).filter((entry) => entry.item.kind === "pr_open");
  expect(entries.map((entry) => ({ run: entry.item.run, ready: entry.readyToMerge, bypass: entry.bypassReview }))).toEqual([
    { run: 9, ready: true, bypass: true },
  ]);
  readiness.set(runKey(DAEMON, 9), { ready: false, reason: "review_not_approved" });
  expect(needsYouEntries(needsYouItems, readiness, NOW).filter((entry) => entry.item.kind === "pr_open")).toEqual([]);
});
