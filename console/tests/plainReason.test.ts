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

test("a reason from no known family shows its first line up to the first semicolon", () => {
  const raw = "merge of task/ko-260-x into main conflicted on: holophyte/loop.py; branch and worktree preserved\nsecond line";
  expect(plainReason(raw)).toEqual({
    sentence: "merge of task/ko-260-x into main conflicted on: holophyte/loop.py",
    branch: null,
    sha: null,
  });
  expect(plainReason("KeyError: 'branch' (at holophyte/loop.py:1201)").sentence).toBe("KeyError: 'branch' (at holophyte/loop.py:1201)");
});
