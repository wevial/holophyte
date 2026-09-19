"""Existing setup cases shared by test_claim's worktree test class.

Kept as a mixin so the required test_claim command still runs every case
while its new environment acceptance tests stay within the module ceiling.
"""
import subprocess

from fake_agent import APPROVE, Commit
from loop_fixture import BRANCH, StubProvider, a_task

import holophyte.claim


class WorktreeSetupCases:
    def test_setup_enables_hooks_only_when_directory_exists(self):
        wt = self.target.parent / "hooks-worktree"
        self.git("worktree", "add", "--detach", str(wt))
        for present in (False, True):
            with self.subTest(present=present):
                self.configure('[worktree]\nsetup = ["mkdir .githooks"]\n'
                               if present else '')
                self.assertTrue(holophyte.claim.run_worktree_setup(self.tgt, wt)[0])
                result = subprocess.run(
                    ["git", "config", "--get", "core.hooksPath"],
                    cwd=wt, capture_output=True, text=True)
                self.assertEqual(result.stdout.strip(), ".githooks" if present else "")
                self.assertEqual(result.returncode, 0 if present else 1)

    def test_setup_runs_in_the_fresh_worktree_before_the_implementer(self):
        """The commands run in the task worktree — not the main checkout —
        while the branch is cut and before any agent turn, and the run merges
        as it otherwise would."""
        marker = self.target.parent / "where.txt"
        self.configure(f'[worktree]\nsetup = ["pwd > {marker}"]\n')

        fake, guard = self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(marker.read_text().strip(),
                         str((self.worktrees / "ko-131-add-a-thing").resolve()))
        self.assertEqual(guard.spawned, [])
        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertIn("the scripted work", self.subjects())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_failed_setup_fails_the_run_before_any_agent_turn(self):
        """No agent is dispatched — the script is empty, so a turn would raise
        — main is untouched, and the branch is discarded rather than preserved:
        nothing was implemented on it."""
        provider = StubProvider(a_task(1), a_task(2))
        self.configure('[worktree]\nsetup = ["echo no toolchain here; exit 3"]\n')

        fake, guard = self.loop(provider=provider)

        self.assertEqual(fake.roles, [])
        self.assertEqual(guard.spawned, [])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertNotIn(BRANCH, self.branches())
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        # A toolchain outage says nothing about the ticket: no agent ran, so
        # the failure must not spend one of its escalation strikes.
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(len(provider.queue), 1)  # the loop stopped
        # The ticket carries the reason, with the failing command and what it
        # printed — a run that ended before anything ran leaves no other trace.
        (_, body), = provider.comments
        self.assertIn("worktree setup", body)
        self.assertIn("no toolchain here", body)
        self.assertIn("exit 3", body)

    def test_a_failing_setup_leaves_a_reused_worktree_as_found(self):
        """A setup failure says nothing about the preserved work a reused
        worktree may hold; only a branch the run cut fresh is discarded."""
        wt = self.worktrees / "ko-131-add-a-thing"
        self.git("worktree", "add", "--detach", str(wt), "main")
        self.git("checkout", "-b", BRANCH, cwd=wt)
        (wt / "rescued.txt").write_text("rescued work\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", "rescued: preserved work", cwd=wt)
        self.configure('[worktree]\nsetup = ["exit 3"]\n')

        self.loop()

        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertTrue((wt / "rescued.txt").exists())
        self.assertIn("rescued: preserved work", self.subjects(BRANCH))
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("left in place", reason)

    def test_the_setup_phase_is_recorded_between_cutting_and_working(self):
        self.configure('[worktree]\nsetup = ["true"]\n')

        self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(self.transitions()[:3],
                         ["claimed -> working", "working -> working",
                          "working -> verifying"])
        (note,) = [summary for (summary,) in
                   self.read("SELECT summary FROM runEvents ORDER BY seq")
                   if "worktree setup" in summary]
        self.assertIn("1 command(s)", note)
