import { expect, test } from "bun:test";
import type { Round, RunDetail, RunEvent, SteerNote } from "../../src/lib/contract.gen";
import { runActivity, type ActivityGroup } from "../../src/app/activity";
import { SERVE } from "./needsFleet";

const served = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const [servedRound] = served.rounds;

const T = Date.UTC(2026, 9, 22, 14, 0);
const MIN = 60_000;
const SEC = 1_000;

const phase = (at: number, summary: string): RunEvent => ({ at, kind: "phase_change", summary });
const event = (at: number, kind: string, summary: string): RunEvent => ({ at, kind, summary });
const finding = (severity: string, message: string) => ({ ...servedRound!.findings[0]!, severity, message });
const criterion = (n: number, status: "met" | "not_met" | "unwitnessed") => ({ id: `C${n}`, n, status, note: `C${n} ${status}` });

function round(fields: Partial<Round>): Round {
  return { ...servedRound!, instructions: [], operator_notes: [], criteria: [], findings: [], ...fields } as Round;
}

function detail(fields: Partial<RunDetail["run"]>, events: RunEvent[], rounds: Round[] = [], steer_notes: SteerNote[] = []): RunDetail {
  return {
    ...served,
    run: { ...served.run, id: 12, phase: "done", started_ms: T, ended_ms: T + 60 * MIN, outcome: "merged", max_rounds: 3, ...fields },
    events,
    rounds,
    steer_notes,
    criteria: [],
    precheck: null,
  };
}

const ROUND_ONE_EVENTS = [
  phase(T, "claimed -> working: cutting task/x"),
  phase(T + 10 * MIN, "working -> verifying"),
  phase(T + 10 * MIN + 30 * SEC, "verifying -> reviewing: round 1 review"),
  phase(T + 13 * MIN + 30 * SEC, "reviewing -> addressing"),
  phase(T + 15 * MIN + 30 * SEC, "addressing -> verifying"),
];

const roundOne = (fields: Partial<Round> = {}) =>
  round({
    round: 1,
    started_ms: T + 10 * MIN + 30 * SEC,
    ended_ms: T + 13 * MIN + 30 * SEC,
    verdict: "changes_requested",
    reviewer_model: "reviewer",
    findings: [finding("p2", "one"), finding("p2", "two"), finding("p2", "three")],
    ...fields,
  });

const joined = (group: ActivityGroup) => group.sub.join(" · ");

test("implement runs to the first verify and a review round holds its verify, review and fix", () => {
  const { groups } = runActivity(detail({}, ROUND_ONE_EVENTS, [roundOne()]), [], T + 60 * MIN);
  expect(groups.map((group) => [group.kind, group.ms])).toEqual([
    ["implement", 10 * MIN],
    ["round", 5 * MIN + 30 * SEC],
  ]);
  const [, first] = groups;
  expect(first!.round?.round).toBe(1);
  expect([joined(first!), first!.tone]).toEqual(["3 should-fix", "warn"]);
  expect(first!.steps.map((step) => [step.text.split(" · ")[0], step.ms])).toEqual([
    ["Verify", 30 * SEC],
    ["Review", 3 * MIN],
    ["Fix", 2 * MIN],
  ]);
});

test("a round's marks are its criteria not met or unwitnessed, in order", () => {
  const criteria = [criterion(1, "not_met"), criterion(2, "unwitnessed"), criterion(3, "met")];
  const { groups } = runActivity(detail({}, ROUND_ONE_EVENTS, [roundOne({ criteria })]), [], T + 60 * MIN);
  const first = groups.find((group) => group.kind === "round")!;
  expect(first.marks.map((mark) => [mark.id, mark.status])).toEqual([
    ["C1", "not_met"],
    ["C2", "unwitnessed"],
  ]);
});

test("a round past the cap is the final check, passed or failed by its criteria", () => {
  const events = [
    phase(T, "claimed -> working"),
    phase(T + 10 * MIN, "working -> verifying"),
    phase(T + 11 * MIN, "verifying -> reviewing: terminal adjudication"),
  ];
  const fourth = (verdict: string, status: "met" | "unwitnessed") =>
    round({ round: 4, started_ms: T + 11 * MIN, ended_ms: T + 12 * MIN, verdict, criteria: [criterion(1, status), criterion(2, status)] });
  const read = (verdict: string, status: "met" | "unwitnessed") => {
    const group = runActivity(detail({}, events, [fourth(verdict, status)]), [], T + 60 * MIN).groups.at(-1)!;
    return [group.kind, group.title, group.steps.map((step) => step.text.split(" · ")[0]), joined(group), group.tone];
  };
  expect(read("pass", "met")).toEqual(["final", "Final check", ["Verify", "Adjudication"], "adjudication passed, 2 of 2 met", "ok"]);
  expect(read("changes_requested", "unwitnessed")).toEqual(["final", "Final check", ["Verify", "Adjudication"], "adjudication FAIL · 2 unwitnessed", "bad"]);
});

test("a live run's implement is running and the newest turn summary is its now line; an ended run has none", () => {
  const events = [
    phase(T, "claimed -> working"),
    event(T + MIN, "turn_progress", "Implement turn: 2 tool calls; last: reading a file"),
    event(T + 2 * MIN, "turn_progress", "Implement turn: 5 tool calls; last: editing a file"),
    event(T + 3 * MIN, "turn_progress", "Implement turn: 9 tool calls; last: running a command"),
  ];
  const live = runActivity(detail({ phase: "working", ended_ms: null, outcome: null }, events), [], T + 4 * MIN);
  expect(live.groups.flatMap((group) => group.steps)).toEqual([]);
  expect(live.groups.map((group) => [group.kind, group.running, group.tone, group.ms])).toEqual([["implement", true, "live", null]]);
  expect(live.now).toEqual({ what: "Implement", since: T, detail: "Implement turn: 9 tool calls; last: running a command" });
  expect(runActivity(detail({ ended_ms: T + 4 * MIN, outcome: "failed" }, events), [], T + 5 * MIN).now).toBeNull();
});

test("the pull request's writing, push and opening are one step of one group", () => {
  const events = [
    event(T + 20 * MIN, "pull_request", "writing the pull request text for task/x from the diff"),
    event(T + 21 * MIN, "pull_request", "pushing task/x to origin and opening its pull request"),
    event(T + 22 * MIN, "pull_request", "pull request open: https://github.com/o/r/pull/737"),
    event(T + 23 * MIN, "babysit_step", "parked"),
  ];
  const { groups } = runActivity(detail({}, events), [], T + 60 * MIN);
  const prs = groups.filter((group) => group.kind === "pull_request");
  expect(prs.map((group) => group.title)).toEqual(["Pull request #737"]);
  expect(prs[0]!.steps.map((step) => step.text)).toEqual(["PR text written from the diff · pushed · opened", "parked"]);
});

test("a babysit pass groups its pickup, threads, checks and CI fix, and names the failing check now", () => {
  const url = "https://github.com/o/r/pull/2400";
  const events = [
    event(T, "pull_request", `resuming run 392's candidate holo/x at f1da4c08e5f4 on ${url} for another babysit pass`),
    event(T + MIN, "babysit_step", "threads"),
    event(T + 4 * MIN, "babysit_step", "checks"),
    event(T + 5 * MIN, "check_fix_round", `check fix round 1 of 2 on ${url}: vitest; failing: no failure line read from the job logs`),
    event(T + 6 * MIN, "babysit_step", "fix"),
  ];
  const threads = round({ round: 1, started_ms: T + 2 * MIN, ended_ms: T + 3 * MIN, findings: [finding("p2", "rename"), finding("nit", "typo")] });
  const live = detail({ phase: "merge_gate", ended_ms: null, outcome: null }, events, [threads]);
  const { groups, now } = runActivity(live, [], T + 7 * MIN);
  expect(groups.map((group) => [group.kind, group.title, joined(group), group.running])).toEqual([
    ["pickup", "Picked up PR #2400", "run 392's candidate at f1da4c0", false],
    ["threads", "Review threads", "2 threads", false],
    ["checks", "Checks", "", false],
    ["fix_ci", "Fix CI", "round 1 of 2", true],
  ]);
  expect(groups[1]!.round).toBe(threads);
  expect(groups[2]!.steps.map((step) => step.text)).toEqual(["vitest failed · no failure line read from the job logs → CI fix round 1 of 2"]);
  expect(now?.what).toBe("Fixing CI · vitest");
});

test("notes read before the run open a Before group and a note during it is a step of the open group", () => {
  const note = { author: "maintainer", run: 12, consumed_by: 12, interrupt: false, withdrawn: false };
  const notes: SteerNote[] = [
    { ...note, id: 1, kind: "amendment", note: "reuse --act", at: T - 60 * MIN, consumed_ms: T },
    { ...note, id: 2, kind: "hint", note: "the test lives in test_sweep", at: T + 5 * MIN, consumed_ms: T + 6 * MIN },
  ];
  const { groups } = runActivity(detail({}, ROUND_ONE_EVENTS, [roundOne()], notes), [], T + 60 * MIN);
  expect([groups[0]!.kind, groups[0]!.title, joined(groups[0]!)]).toEqual(["before", "Before this run", "1 amendment carried in"]);
  const implement = groups.find((group) => group.kind === "implement")!;
  expect(implement.steps.map((step) => step.text)).toEqual(["You hinted: the test lives in test_sweep"]);
});

test("a round's other events are quiet steps, but not commit sightings or phase changes", () => {
  const events = [
    ...ROUND_ONE_EVENTS,
    event(T + 11 * MIN, "warning", "reviewer slow to start"),
    event(T + 11 * MIN, "pr_seen_commits", "abc123"),
    phase(T + 12 * MIN, "working -> working"),
  ];
  const first = runActivity(detail({}, events, [roundOne()]), [], T + 60 * MIN).groups.find((group) => group.kind === "round")!;
  expect(first.steps.filter((step) => step.quiet).map((step) => step.text)).toEqual(["reviewer slow to start"]);
  expect(first.steps.map((step) => step.text)).not.toContain("abc123");
  expect(first.steps.some((step) => step.text.includes("working"))).toBe(false);
});

test("a round the babysitter records after a checks step stays inside Checks with the check steps", () => {
  const url = "https://github.com/o/r/pull/2400";
  const events = [
    event(T, "pull_request", `resuming run 392's candidate holo/x at f1da4c08e5f4 on ${url} for another babysit pass`),
    event(T + MIN, "babysit_step", "checks"),
    event(T + 3 * MIN, "check_rerun", `rerunning the failed jobs of vitest on ${url}: workflow run(s) 7`),
    event(T + 5 * MIN, "check_fix_round", `check fix round 1 of 2 on ${url}: vitest; failing: expected 2`),
  ];
  const bookkeeping = round({ round: 1, started_ms: T + 2 * MIN, ended_ms: T + 2 * MIN, verdict: "pass" });
  const { groups } = runActivity(detail({}, events, [bookkeeping]), [], T + 60 * MIN);
  expect(groups.map((group) => group.kind)).toEqual(["pickup", "checks"]);
  expect(groups[1]!.round).toBe(bookkeeping);
  expect(groups[1]!.steps.map((step) => step.text)).toEqual(["vitest rerun", "vitest failed again · expected 2 → CI fix round 1 of 2"]);
});

test("a verify that steps into another verify keeps its first start and its pre-check", () => {
  const events = [
    phase(T, "claimed -> working"),
    phase(T + 10 * MIN, "working -> verifying: round 1: verify before review"),
    event(T + 10 * MIN + 10 * SEC, "witness_precheck", "witness pre-check at abc: 1 of 2 criteria witnessed\ncriterion C2 names no test file"),
    phase(T + 10 * MIN + 20 * SEC, "verifying -> verifying: round 1: witness turn 1 of 2"),
    phase(T + 11 * MIN, "verifying -> reviewing: round 1 review"),
  ];
  const first = round({ round: 1, started_ms: T + 11 * MIN, ended_ms: T + 12 * MIN, verdict: "pass" });
  const { groups } = runActivity(detail({}, events, [first]), [], T + 60 * MIN);
  const verify = groups.find((group) => group.kind === "round")!.steps[0]!;
  expect([verify.at, verify.ms, verify.text]).toEqual([T + 10 * MIN, MIN, "Verify · 1 of 2 witnessed · C2"]);
});

test("a Review threads group marks its round's criteria not met or unwitnessed", () => {
  const events = [event(T + MIN, "babysit_step", "threads")];
  const threads = round({ round: 2, started_ms: T + 2 * MIN, ended_ms: T + 3 * MIN, criteria: [criterion(1, "met"), criterion(2, "unwitnessed")] });
  const group = runActivity(detail({}, events, [threads]), [], T + 60 * MIN).groups.find((group) => group.kind === "threads")!;
  expect(group.marks.map((mark) => [mark.id, mark.status])).toEqual([["C2", "unwitnessed"]]);
});
