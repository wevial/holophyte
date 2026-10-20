import { expect, test } from "bun:test";
import { rowFacts } from "../../src/next/prRow";

test("a row's facts colour only what is off and name a change request only when one stands", () => {
  expect(rowFacts({ checks: "success", open_threads: 0, review: "review_required" })).toEqual([
    { label: "checks ✓", tone: "ok" },
    { label: "0 threads", tone: "neutral" },
  ]);
  expect(rowFacts({ checks: "failure", open_threads: 3, review: "changes_requested" })).toEqual([
    { label: "checks failing", tone: "bad" },
    { label: "3 open threads", tone: "warn" },
    { label: "changes requested", tone: "bad" },
  ]);
  expect(rowFacts({ checks: null, open_threads: null, review: null })).toEqual([
    { label: "checks unknown", tone: "neutral" },
    { label: "threads unknown", tone: "neutral" },
  ]);
});

test("a row's facts add its review rounds and, in warn, each pull request or ticket it waits on, only when sent", () => {
  const pr = { checks: "success", open_threads: 0, review: "review_required" };
  const waits_on = [
    { ticket: "KO-1", pr_url: "https://github.com/example/repo/pull/2375" },
    { ticket: "KO-3", pr_url: null },
  ];
  expect(rowFacts(pr, { round: 6, waits_on })).toEqual([
    { label: "checks ✓", tone: "ok" },
    { label: "0 threads", tone: "neutral" },
    { label: "6 rounds", tone: "neutral" },
    { label: "waits on PR #2375", tone: "warn" },
    { label: "waits on KO-3", tone: "warn" },
  ]);
  expect(rowFacts(pr, { round: 1 })).toEqual([
    { label: "checks ✓", tone: "ok" },
    { label: "0 threads", tone: "neutral" },
    { label: "1 round", tone: "neutral" },
  ]);
  expect(rowFacts(pr, {})).toEqual([
    { label: "checks ✓", tone: "ok" },
    { label: "0 threads", tone: "neutral" },
  ]);
});

test("a row's facts end in its diff size when all three counts are sent, after its rounds and what it waits on", () => {
  const pr = { checks: "success", open_threads: 0, review: "review_required", changed_files: 13, additions: 718, deletions: 85 };
  expect(rowFacts(pr, { round: 4, waits_on: [{ ticket: "KO-3", pr_url: null }] })).toEqual([
    { label: "checks ✓", tone: "ok" },
    { label: "0 threads", tone: "neutral" },
    { label: "4 rounds", tone: "neutral" },
    { label: "waits on KO-3", tone: "warn" },
    { label: "13 files +718 −85", tone: "neutral" },
  ]);
  expect(rowFacts({ ...pr, changed_files: 1, additions: 3, deletions: 0 })).toEqual([
    { label: "checks ✓", tone: "ok" },
    { label: "0 threads", tone: "neutral" },
    { label: "1 file +3 −0", tone: "neutral" },
  ]);
  expect(rowFacts({ ...pr, deletions: null })).toEqual([
    { label: "checks ✓", tone: "ok" },
    { label: "0 threads", tone: "neutral" },
  ]);
});
