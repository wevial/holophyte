"""A woken pull-request run's fix round, and a fix round that makes no commit."""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_reconcile  # noqa: E402 - after the sys.path insert above
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    REQUEST_CHANGES,
    Commit,
    Idle,
    Reply,
)
from loop_fixture import MergeModeFixture  # noqa: E402


@dataclass
class TurnsGreen(Idle):
    """A fix turn that commits nothing; its head's checks pass from then on."""

    green: list = field(default_factory=list)

    def play(self, cwd, turn):
        self.green.append(True)
        return super().play(cwd, turn)


class WokenFixNoteTests(MergeModeFixture):
    parked = test_reconcile.CiParkTests
    OLD, ci_parked, pull = parked.OLD, parked.ci_parked, parked.pull
    github, age, reconcile = parked.github, parked.age, parked.reconcile

    def test_a_red_check_wake_reason_is_not_a_review_fix_note(self):
        self.ci_parked("PENDING")
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       'review_fixes = true\n')
        self.age()
        self.reconcile(self.pull(self.OLD, "FAILURE"))
        ((wake,),) = self.read("SELECT summary FROM runEvents"
                               " WHERE kind = 'ci_wake'")
        self.serve(self.pr_state([self.DEFECT]), self.pr_state())
        fake, _ = self.loop(Reply("THREAD 1: ADDRESS -- a real crash"),
                            Commit("thread fix"), REQUEST_CHANGES,
                            Idle("nothing asks for a change"),
                            provider=self.provider())
        self.assertEqual(fake.roles,
                         ["adjudicate", "implement", "review", "implement"])
        ((resumed,),) = self.read("SELECT summary FROM runEvents WHERE kind ="
                                  " 'pull_request' AND summary LIKE 'resuming%'")
        candidate = resumed.split(" at ")[1].split()[0]
        fixed = fake.turns[2].goal.split("Review commit ")[1].split()[0]
        self.assertFalse(fixed.startswith(candidate))
        self.assertTrue(self.read("SELECT 1 FROM runEvents WHERE summary LIKE"
                                  f" '%Addressed in {fixed}%'"))
        goals = [turn.goal for role, turn in zip(fake.roles, fake.turns)
                 if role == "implement"]
        self.assertTrue(goals[1].startswith("Fix the pre-merge review findings"))
        self.assertFalse([goal for goal in goals if wake in goal])
        self.assertEqual(self.read("SELECT summary FROM runEvents"
                                   " WHERE kind = 'review_fix_allowance'"), [])


class NoCommitCheckFixTests(MergeModeFixture):
    UNIT = {"name": "unit", "status": "completed", "conclusion": "failure",
            "html_url": "https://github.com/example/repo/actions/runs/5/job/42",
            "id": 42, "app": {"slug": "github-actions"}}

    def test_a_no_commit_check_fix_on_a_head_that_turns_green_parks_for_the_merge(
            self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route()
        fix = TurnsGreen("The unit failure is a flaky runner, not this change.")
        jobs = iter(range(42, 10**4))
        def check_runs(target, pull, sha):
            return [dict(self.UNIT, conclusion="success") if fix.green
                    else dict(self.UNIT, id=next(jobs))]
        self.enterContext(patch("holophyte.pr.pr_status._check_runs_of",
                                check_runs))
        self.loop(Commit("the scripted work"), APPROVE, Idle(""), fix,
                  provider=self.provider())
        self.assertEqual(self.read("SELECT phase, parkKind FROM runs"),
                         [("awaiting_merge_approval", "pull_request")])
        self.assertIn(fix.reply, self.question())


if __name__ == "__main__":
    unittest.main()
