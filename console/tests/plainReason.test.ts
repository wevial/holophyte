import { expect, test } from "bun:test";
import { plainReason } from "../src/lib/attention";

// Each fixture line is one `RunFailure` message as holophyte/loop.py raises
// it, lifted from a real run's `outcomeReason`.

test("a terminal adjudication reads as the verdict, with the branch and whole sha", () => {
  const raw = "terminal adjudication: FAIL; branch task/ko-343-the-loop-runs-a-pool-of-worker preserved at 046d7d70f5e1";
  expect(plainReason(raw)).toEqual({
    sentence: "Review adjudicated FAIL",
    branch: "task/ko-343-the-loop-runs-a-pool-of-worker",
    sha: "046d7d70f5e1",
  });
});

test("a leftover worktree whose preserved commits conflict with main reads as the conflict, with the branch and sha", () => {
  const raw =
    "preserved commits on task/ko-301-the-sweep-frees-a-lease conflict with a main that moved on and the implementer left the merge unresolved in holophyte/loop.py, tests/test_sweep.py; a human resolves the merge before this ticket is run again; branch task/ko-301-the-sweep-frees-a-lease preserved at 9b1c2d3e4f50";
  expect(plainReason(raw)).toEqual({
    sentence: "Preserved branch conflicts with the moved main",
    branch: "task/ko-301-the-sweep-frees-a-lease",
    sha: "9b1c2d3e4f50",
  });
});

test("a failed verify reads as the command failing, whether or not the line names a branch", () => {
  expect(plainReason("verify failed before merge; branch task/ko-229-rename preserved at 7f2e1a0b9c8d")).toEqual({
    sentence: "Verify command failed",
    branch: "task/ko-229-rename",
    sha: "7f2e1a0b9c8d",
  });
  expect(plainReason("verify failed: 2 tests errored in test_store_surface after the rename")).toEqual({
    sentence: "Verify command failed",
    branch: null,
    sha: null,
  });
});

test("a task past its budget names the budget, with the branch the work was kept on", () => {
  expect(plainReason("implementer exceeded the 45 min budget; work kept on task/ko-318-the-console-polls at c0ffee123456")).toEqual({
    sentence: "Ran past its 45 min budget",
    branch: "task/ko-318-the-console-polls",
    sha: "c0ffee123456",
  });
});

test("an implementer that exited without committing reads as such, on a fresh or a reused worktree", () => {
  expect(plainReason("implementer made no commits; the empty branch and worktree were discarded").sentence).toBe(
    "Implementer exited without committing",
  );
  expect(plainReason("implementer made no new commits; preserved work kept on task/ko-250-a-crash at deadbeef1234")).toEqual({
    sentence: "Implementer exited without committing",
    branch: "task/ko-250-a-crash",
    sha: "deadbeef1234",
  });
});

test("an implementer that died reads as the crash, named by the exception's type", () => {
  // `crash_reason()`'s line for run 103 (KO-273): the type, the message, the factory frame.
  expect(plainReason("OperationalError: database is locked (at holophyte/runs.py:record_round:212)")).toEqual({
    sentence: "Run crashed with OperationalError",
    branch: null,
    sha: null,
  });
  // `sh()` failing under the implementer turn, whitespace collapsed to one line.
  expect(
    plainReason("RuntimeError: `['git', 'rev-parse', 'HEAD']` failed: fatal: not a git repository (at holophyte/loop.py:_implement:913)").sentence,
  ).toBe("Run crashed with RuntimeError");
  // No factory frame on the traceback: the bare `TYPE: message` form.
  expect(plainReason("KeyError: 'branch'").sentence).toBe("Run crashed with KeyError");
});

test("a reason from no known family shows its first line up to the first semicolon", () => {
  const raw = "merge of task/ko-260-x into main conflicted on: holophyte/loop.py; branch and worktree preserved\nsecond line";
  expect(plainReason(raw)).toEqual({
    sentence: "merge of task/ko-260-x into main conflicted on: holophyte/loop.py",
    branch: null,
    sha: null,
  });
  expect(plainReason("[merge] after command failed with exit 2: bun run build; branch task/ko-260-x preserved at 1234567abcdef").sentence).toBe(
    "[merge] after command failed with exit 2: bun run build",
  );
});
