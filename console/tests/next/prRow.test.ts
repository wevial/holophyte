import { expect, test } from "bun:test";
import { rowFacts } from "../../src/next/prRow";

test("a row's facts colour only what is off and name a change request only when one stands", () => {
  expect(rowFacts({ checks: "success", open_threads: 0, review: "review_required" })).toEqual([
    { label: "checks green", tone: "ok" },
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
