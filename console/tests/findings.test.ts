import { expect, test } from "bun:test";
import { findingParts, findingPath, findingsHistory, severityOf } from "../src/lib/findings";
import type { LedgerRow } from "../src/lib/ledger";
import type { Finding, Round } from "../src/lib/types";

const finding = (over: Partial<Finding>): Finding => ({
  path: "holophyte/loop.py",
  severity: "p2",
  message: "",
  ...over,
});

test("severityOf reads the reviewer's p0/p1/p2/nit as must/must/should/nit", () => {
  for (const [severity, pill] of [
    ["p0", "must"],
    ["p1", "must"],
    ["P1", "must"],
    ["p2", "should"],
    ["nit", "nit"],
    ["anything-else", "nit"],
  ] as const) {
    expect(severityOf(finding({ severity }))).toBe(pill);
  }
});

test("findingPath strips either container mount and leaves a repository path alone", () => {
  expect(findingPath(finding({ path: "/workspace/holophyte/loop.py" }))).toBe("holophyte/loop.py");
  expect(findingPath(finding({ path: "/home/reviewer/candidate/holophyte/loop.py" }))).toBe("holophyte/loop.py");
  expect(findingPath(finding({ path: "holophyte/loop.py" }))).toBe("holophyte/loop.py");
});

test("a [P1] bullet splits into the bold lead, a dropped location link and the remainder", () => {
  const parts = findingParts(
    "- [P1] **Conflicted merge fails the run instead of returning to the implementer** — " +
      "[holophyte/loop.py](/home/reviewer/candidate/holophyte/loop.py:345) returns before " +
      "`release()` runs on the conflict path",
  );
  expect(parts.title).toBe("Conflicted merge fails the run instead of returning to the implementer");
  expect(parts.body).toBe("returns before `release()` runs on the conflict path");
  expect(parts.criterion).toBeNull();
});

test("a plain sentence has no title and its message is the body", () => {
  const parts = findingParts("The merge commit message does not name the conflicted ref");
  expect(parts.title).toBeNull();
  expect(parts.body).toBe("The merge commit message does not name the conflicted ref");
  expect(parts.criterion).toBeNull();
});

test("a CRITERION line titles Criterion n · status with the reason as body and the criterion folded", () => {
  const parts = findingParts(
    "CRITERION 2: not met — no test exercises the conflict path\n" +
      "Given a merge-gate conflict, when the gate runs, then the run goes back " +
      "to the implementer (tests/test_gates.py witnesses it)",
  );
  expect(parts.title).toBe("Criterion 2 · not met");
  expect(parts.body).toBe("no test exercises the conflict path");
  expect(parts.criterion).toBe(
    "Given a merge-gate conflict, when the gate runs, then the run goes back " +
      "to the implementer (tests/test_gates.py witnesses it)",
  );
});

const T = 1_756_900_000_000;

// KO-372's run 228, lifted: three findings in round 1, one in round 2 and
// the approve in round 3.
const A = finding({
  path: "holophyte/loop.py",
  line: 345,
  severity: "p0",
  message: "- [P0] **Merge gate conflict fails the run outright** — the gate returns before `release()` runs",
});
const B = finding({ path: "holophyte/serve.py", line: 12, severity: "p2", message: "Trailing comma in the route table" });
const C = finding({ path: "store/__init__.py", line: 60, severity: "nit", message: "Ledger write is not transactional" });
const ROUNDS: Round[] = [
  { round: 1, started_ms: T, ended_ms: T + 60_000, verdict: "changes_requested", findings: [A, B, C] },
  { round: 2, started_ms: T + 120_000, ended_ms: T + 180_000, verdict: "changes_requested", findings: [C] },
  { round: 3, started_ms: T + 240_000, ended_ms: T + 300_000, verdict: "pass", findings: [] },
];

test("findingsHistory marks a finding absent next round fixed, one still standing open, and counts 4 over 3 rounds", () => {
  const history = findingsHistory(ROUNDS, []);
  expect(history.map((group) => group.round)).toEqual([3, 2, 1]);
  expect(history.reduce((sum, group) => sum + group.findings.length, 0)).toBe(4);
  expect(history.length).toBe(3);
  // Round 3 approved: no findings, so no fates.
  expect(history[0]!.findings).toEqual([]);
  // Round 2's only finding is absent from the approving round: fixed in
  // round 3.
  expect(history[1]!.findings.map(({ finding, fate }) => [finding.path, fate])).toEqual([
    ["store/__init__.py", "fixed"],
  ]);
  // Round 1: A and B are gone from round 2 (fixed there); C is still
  // raised, so it was open after the round.
  const fates = new Map(history[2]!.findings.map(({ finding, fate }) => [finding.path, fate]));
  expect(fates.get("holophyte/loop.py")).toBe("fixed");
  expect(fates.get("holophyte/serve.py")).toBe("fixed");
  expect(fates.get("store/__init__.py")).toBe("open");
});

test("a DECLINE or FOLLOW_UP line in the round's ledger row sets the fate and becomes the sentence", () => {
  const ledger: LedgerRow[] = [
    {
      at: T + 90_000,
      run: 228,
      ticket: "KO-372",
      kind: "round",
      source: "loop",
      text:
        "Round 1: REQUEST_CHANGES -> fix round\n" +
        "Reviewer findings:\nthe verdict text\n\n" +
        "Implementer response:\n" +
        "ADDRESS holophyte/loop.py — the gate now returns to the implementer\n" +
        "DECLINE holophyte/serve.py — the comma is house style\n" +
        "FOLLOW_UP Ledger write is not transactional — filed KO-441",
    },
  ];
  const history = findingsHistory(ROUNDS, ledger);
  const byPath = new Map(history[2]!.findings.map((entry) => [entry.finding.path, entry]));
  // B's path is named on the DECLINE line; it stays declined even though
  // round 2 no longer raises it.
  expect(byPath.get("holophyte/serve.py")!.fate).toBe("declined");
  expect(byPath.get("holophyte/serve.py")!.sentence).toBe("DECLINE holophyte/serve.py — the comma is house style");
  // C is named by its first words on the FOLLOW_UP line.
  expect(byPath.get("store/__init__.py")!.fate).toBe("follow_up");
  expect(byPath.get("store/__init__.py")!.sentence).toBe(
    "FOLLOW_UP Ledger write is not transactional — filed KO-441",
  );
  // A's ADDRESS line is no adjudication the card reports; absent from
  // round 2 it is fixed.
  expect(byPath.get("holophyte/loop.py")!.fate).toBe("fixed");
  expect(byPath.get("holophyte/loop.py")!.sentence).toBeNull();
});

test("a bulleted DECLINE or FOLLOW_UP line still sets the fate and sentence", () => {
  const ledger: LedgerRow[] = [
    {
      at: T + 90_000,
      run: 228,
      ticket: "KO-372",
      kind: "round",
      source: "loop",
      text:
        "Round 1: REQUEST_CHANGES -> fix round\n" +
        "Reviewer findings:\nthe verdict text\n\n" +
        "Implementer response:\n" +
        "- DECLINE holophyte/serve.py — the comma is house style\n" +
        "2. FOLLOW_UP Ledger write is not transactional — filed KO-441",
    },
  ];
  const history = findingsHistory(ROUNDS, ledger);
  const byPath = new Map(history[2]!.findings.map((entry) => [entry.finding.path, entry]));
  expect(byPath.get("holophyte/serve.py")!.fate).toBe("declined");
  expect(byPath.get("holophyte/serve.py")!.sentence).toBe(
    "- DECLINE holophyte/serve.py — the comma is house style",
  );
  expect(byPath.get("store/__init__.py")!.fate).toBe("follow_up");
  expect(byPath.get("store/__init__.py")!.sentence).toBe(
    "2. FOLLOW_UP Ledger write is not transactional — filed KO-441",
  );
});

test("a DECLINE citing path:line names only that line's finding, not the same path's others", () => {
  const c1 = finding({ path: "criteria.md", line: 1, message: "First criterion is vague" });
  const c10 = finding({ path: "criteria.md", line: 10, message: "Tenth criterion is out of scope" });
  const rounds: Round[] = [
    { round: 1, started_ms: T, ended_ms: T + 60_000, verdict: "changes_requested", findings: [c1, c10] },
    { round: 2, started_ms: T + 120_000, ended_ms: T + 180_000, verdict: "pass", findings: [] },
  ];
  const ledger: LedgerRow[] = [
    {
      at: T + 90_000,
      run: 228,
      ticket: "KO-372",
      kind: "round",
      source: "loop",
      text:
        "Round 1: REQUEST_CHANGES -> fix round\n" +
        "Reviewer findings:\nthe verdict text\n\n" +
        "Implementer response:\n" +
        "DECLINE criteria.md:10 — outside the ticket",
    },
  ];
  const history = findingsHistory(rounds, ledger);
  const byLine = new Map(history[1]!.findings.map((entry) => [entry.finding.line, entry]));
  expect(byLine.get(10)!.fate).toBe("declined");
  expect(byLine.get(10)!.sentence).toBe("DECLINE criteria.md:10 — outside the ticket");
  // Round 2 approved without it, so the line-1 finding is fixed — the
  // citation's prefix did not mark it declined.
  expect(byLine.get(1)!.fate).toBe("fixed");
  expect(byLine.get(1)!.sentence).toBeNull();
});

test("a finding in the last round of a run that ended unmerged is open", () => {
  const failed: Round[] = [
    ROUNDS[0]!,
    { round: 2, started_ms: T + 120_000, ended_ms: T + 180_000, verdict: "changes_requested", findings: [C] },
  ];
  const history = findingsHistory(failed, []);
  expect(history[0]!.round).toBe(2);
  expect(history[0]!.findings[0]!.fate).toBe("open");
});
