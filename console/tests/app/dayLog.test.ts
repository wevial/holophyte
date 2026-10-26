import { expect, test } from "bun:test";
import { categoryOf, phraseOf } from "../../src/app/dayLog";
import type { LedgerEntry } from "../../src/lib/contract.gen";

const PR = "https://github.com/kovial/holophyte/pull/896";

const row = (kind: string, source: string, text: string, extra: Partial<LedgerEntry> = {}): LedgerEntry => ({
  at: 0, run: 2171, ticket: "HOLO-594", pr_url: PR, kind, source, text, ...extra,
});

const SHAPES: [LedgerEntry, string, string, string][] = [
  [row("merge", "loop", "MERGED to main as 5acc138 (branch task/holo-594 deleted)."), "outcomes", "merged", "merged"],
  [row("failure", "loop", "FAILED verify before merge.\n\n$ bun test\n1 fail\n"), "outcomes", "failed", "failed: verify"],
  [row("failure", "loop", `FAILED after the fix round for ${PR}; branch task/holo-594 preserved`), "outcomes", "failed", "failed"],
  [row("failure", "factory", "no heartbeat for 20 minutes", { failure_kind: "swept" }), "outcomes", "failed", "failed: swept"],
  [row("failure", "factory", "the reviewer route refused twice", { failure_kind: "review_route" }), "outcomes", "failed", "failed: review route"],
  [row("note", "loop", `PR OPEN: ${PR}\nchecks pending\nBranch task/holo-594 is pushed at 5acc138`), "outcomes", "pr-opened", "PR opened"],
  [row("park", "factory", "implementer: criterion C7 cannot be met as written: the export has no header field"), "needs-you", "parked", "needs you: C7 contradiction"],
  [row("park", "factory", "a thread needs a human's answer (1 open)"), "needs-you", "parked", "needs you: a thread to answer"],
  [row("park", "factory", "which flag name should ship?"), "needs-you", "parked", "needs you"],
  [row("note", "loop", "MERGE REQUEUED: the ticket drifted from the contract this run"), "needs-you", "merge-requeued", "needs you: merge requeued"],
  [row("intervention", "operator", "human requeue: retry after the flaky check"), "operator", "requeues", "operator requeued"],
  [row("intervention", "operator", "human steer: keep the rail compact"), "operator", "steers", "operator steered"],
  [row("intervention", "operator", "human operator_note: the header names the column"), "operator", "notes", "operator note"],
  [row("intervention", "operator", "human approve: merged on GitHub"), "operator", "approvals", "operator approved"],
  [row("intervention", "operator", "human pause: holding for the release"), "operator", "pause-abort-start", "operator paused"],
  [row("intervention", "operator", "human abort: superseded by HOLO-600"), "operator", "pause-abort-start", "operator aborted"],
  [row("intervention", "operator", "human launch_loop: started from the console"), "operator", "pause-abort-start", "operator started the loop"],
  [row("intervention", "loop", "factory requeue: the contract was amended"), "operator", "factory-amendments", "factory requeued"],
  [row("intervention", "loop", "factory steer: use the named store writer"), "operator", "factory-amendments", "factory steered"],
  [row("round", "loop", "Round 6: APPROVE\nReviewer verdict:\nVERDICT: APPROVE"), "review", "verdicts", "review round 6: approve"],
  [row("round", "loop", "Round 1: REQUEST_CHANGES -> fix round\nReviewer findings:\n- p1 the limit"), "review", "verdicts", "review round 1: changes asked"],
  [row("adjudication", "loop", "Terminal adjudication after 3 review rounds: PASS\n\nAdjudicator reply:\nPASS"), "review", "adjudications", "adjudicated after 3 rounds: pass"],
  [row("note", "loop", "Review cap 3 for 207 changed lines"), "review", "review-caps", "review cap 3 · 207 lines"],
  [row("note", "loop", "Witness pre-check: turn 1 of 2 before round 1"), "review", "witness-pre-checks", "witness pre-check 1 of 2"],
  [row("round", "loop", `Babysit pass 3 over ${PR}: no unresolved threads, checks success`), "babysit", "babysit-passes", "babysit pass 3: checks green"],
  [row("round", "loop", `Babysit pass 1 over ${PR}: no unresolved threads, checks failure`), "babysit", "babysit-passes", "babysit pass 1: checks failed"],
  [row("round", "loop", `Babysit pass 2 over ${PR}: 1 unresolved thread(s), checks pending\n1 opened by a person`), "babysit", "babysit-passes", "babysit pass 2: 1 open thread, checks pending"],
  [row("round", "loop", `Babysit pass 4 over ${PR} at 5acc138e0c2b:`), "babysit", "babysit-passes", "babysit pass 4"],
  [row("round", "loop", `Automatic fix round 1 of 2 after round 6 on ${PR}: the review of the fix at 5acc138 asked for new changes`), "babysit", "fix-rounds", "automatic fix 1 of 2"],
  [row("round", "loop", `Check fix round 1 of 2 on ${PR}: unit failed`), "babysit", "fix-rounds", "check fix 1 of 2"],
  [row("round", "loop", `Capture fix round 1 of 2 on ${PR}: the UI capture failed`), "babysit", "fix-rounds", "capture fix 1 of 2"],
  [row("round", "loop", "Babysit review fix allowance after round 3: one fix and recorded re-review, even if reviewRoundCap is spent."), "babysit", "fix-rounds", "babysit review fix"],
  [row("round", "loop", `Rewrote the description of ${PR} for the review of the fix at 5acc138`), "babysit", "fix-rounds", "description rewritten"],
  [row("note", "loop", `Evidence on ${PR}: captured at 5acc138e0c2b`), "babysit", "evidence", "evidence captured"],
  [row("intervention", "loop", "supervisor babysit: checks finished on a parked pull request"), "babysit", "supervisor-wake-ups", "babysitter woken"],
  [row("intervention", "loop", "supervisor launch_loop: no loop was running", { run: null, ticket: null }), "system", "loop-starts", "loop started by supervisor"],
  [row("intervention", "loop", "supervisor launch_backoff: the loop exited at startup, retrying in 60s", { run: null, ticket: null }), "system", "loop-starts", "loop started by supervisor"],
  [row("note", "loop", "Merged main into task/holo-594 at 9f1e6b3 (GitHub reported a conflict)"), "system", "main-merged", "main merged into branch"],
  [row("note", "loop", "Round 4 adversary concerns (non-blocking):\n- the cache key"), "system", "other", "Round 4 adversary concerns (non-blocking)"],
];

test("each ledger text shape maps to its group, item and short phrase", () => {
  const read = SHAPES.map(([entry]) => [categoryOf(entry).group, categoryOf(entry).item, phraseOf(entry)]);
  expect(read).toEqual(SHAPES.map(([, group, item, phrase]) => [group, item, phrase]));
});

test("a migration row falls to Other, cut at its first colon, and an unlisted human action to Pause, abort, start", () => {
  const migration = row("intervention", "factory", "store schema 41 (migrated from 40 at 2026-10-26T09:12:00+00:00 by abc1234, pid 2801, holo)", {
    run: null, ticket: null, action: "migrate", project: "/srv/holophyte",
  });
  expect(categoryOf(migration)).toEqual({ group: "system", item: "other" });
  expect(phraseOf(migration)).toBe("store schema 41 (migrated from 40 at 2026-10-26T09");
  const decide = row("intervention", "operator", "human decide: option B");
  expect([categoryOf(decide).item, phraseOf(decide)]).toEqual(["pause-abort-start", "operator decided"]);
});
