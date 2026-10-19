"""Retiring a finished ticket's task worktree and branch, with the real git."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    LoopFixture,
    StubProvider,
)

import holophyte.host.worktree_cleanup as worktree_cleanup  # noqa: E402 - after the sys.path insert above
import holophyte.loop.task_worktree as task_worktree  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets as tickets  # noqa: E402 - after the sys.path insert above

BRANCH = "task/holo-1-x"


class WorktreeCleanupFixture(LoopFixture):

    def task_tree(self, branch, on_main=False):
        wt = self.worktrees / branch.split("/", 1)[1]
        self.git("worktree", "add", "-q", "-b", branch, str(wt))
        (wt / "work.txt").write_text(f"{branch}\n")
        self.git("add", ".", cwd=wt)
        self.git("commit", "-q", "-m", f"work on {branch}", cwd=wt)
        if on_main:
            self.git("merge", "-q", "--ff-only", branch)
        return wt

    def resolves(self, ref):
        return self.git("for-each-ref", "--format=%(objectname)", ref).strip()


class RetireWorktreeTests(WorktreeCleanupFixture):

    def test_merged_retire_removes_worktree_and_branch_and_keeps_other_refs(self):
        wt = self.task_tree(BRANCH)
        tip = self.resolves(f"refs/heads/{BRANCH}")
        self.git("update-ref", "refs/holophyte/keep", tip)
        self.assertNotEqual(tip, self.base)

        self.assertIsNone(
            task_worktree.retire_worktree(self.project, BRANCH, merged=True))

        self.assertFalse(wt.exists())
        self.assertNotIn(str(wt), self.git("worktree", "list"))
        self.assertEqual(self.resolves(f"refs/heads/{BRANCH}"), "")
        self.assertEqual(self.resolves("refs/holophyte/keep"), tip)

    def test_untracked_file_keeps_worktree_file_and_branch(self):
        wt = self.task_tree(BRANCH)
        (wt / "scratch.txt").write_text("not committed\n")

        self.assertEqual(
            task_worktree.retire_worktree(self.project, BRANCH, merged=True),
            "uncommitted work")

        self.assertEqual((wt / "scratch.txt").read_text(), "not committed\n")
        self.assertIn(str(wt), self.git("worktree", "list"))
        self.assertNotEqual(self.resolves(f"refs/heads/{BRANCH}"), "")


class RetireFinishedTests(WorktreeCleanupFixture):

    def setUp(self):
        super().setUp()
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        self.project_id = tickets.ensure_project(
            self.conn, StubProvider.TEAM, str(self.target))

    def ticket(self, n, status, ended=True, on_main=False):
        branch = f"task/holo-{n}-x"
        ticket = tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"iss-{n}",
            linear_identifier=f"HOLO-{n}", title=f"ticket {n}",
            acceptance_criteria=["it works"], verification_commands=["true"])
        run_id = store.claim(self.conn, self.project_id, ticket)
        tickets.transition(self.conn, ticket, "in_flight")
        store.set_branch(self.conn, run_id, branch)
        if ended and status == "merged":
            for phase in ("merge_gate", "merging"):
                store.set_phase(self.conn, run_id, phase)
            store.release(self.conn, run_id, "merged")
        elif ended:
            store.release(self.conn, run_id, "failed", "abandoned")
        if status != "in_flight":
            tickets.walk_ticket(self.conn, ticket, status)
        return self.task_tree(branch, on_main=on_main), branch, run_id

    def test_only_finished_tickets_with_ended_runs_are_retired(self):
        merged_wt, merged_branch, _ = self.ticket(1, "merged")
        abandoned_wt, abandoned_branch, _ = self.ticket(2, "abandoned",
                                                        on_main=True)
        open_wt, open_branch, _ = self.ticket(3, "abandoned", ended=False)
        flight_wt, flight_branch, _ = self.ticket(5, "in_flight", ended=False)

        lines = worktree_cleanup.retire_finished(self.project, self.conn)

        self.assertEqual(lines, [
            "retired HOLO-1's worktree and branch task/holo-1-x",
            "retired HOLO-2's worktree and branch task/holo-2-x",
        ])
        for wt, branch in ((merged_wt, merged_branch),
                           (abandoned_wt, abandoned_branch)):
            self.assertFalse(wt.exists())
            self.assertEqual(self.resolves(f"refs/heads/{branch}"), "")
        for wt, branch in ((open_wt, open_branch), (flight_wt, flight_branch)):
            self.assertEqual((wt / "work.txt").read_text(), f"{branch}\n")
            self.assertNotEqual(self.resolves(f"refs/heads/{branch}"), "")

    def test_uncommitted_change_keeps_worktree_and_records_refusal(self):
        wt, _, run_id = self.ticket(4, "merged")
        (wt / "work.txt").write_text("edited, not committed\n")

        lines = worktree_cleanup.retire_finished(self.project, self.conn)

        self.assertEqual(lines, ["kept HOLO-4's worktree: uncommitted work"])
        self.assertEqual((wt / "work.txt").read_text(),
                         "edited, not committed\n")
        self.assertIn(
            (run_id, "kept HOLO-4's worktree: uncommitted work"),
            self.read("SELECT runId, summary FROM runEvents"
                      " WHERE kind = 'worktree_retirement_refused'"))
