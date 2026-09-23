"""KO-707: a red Actions check's failed jobs are rerun once before the fix turn."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # factory.py imports store by name
sys.path.insert(0, str(HERE))  # `discover -s tests` and `-m unittest` alike
import babysit_fixture as cases  # noqa: E402
from fake_agent import APPROVE, Commit, Idle  # noqa: E402
from loop_fixture import MergeModeFixture, StubProvider, a_task  # noqa: E402

RERUN = "repos/example/repo/actions/runs/77/rerun-failed-jobs"


class BabysitRerunTests(cases.BabysitHelpers, MergeModeFixture):
    """A flake goes green on its rerun and keeps the fix turn unspent."""
    UNIT = {"name": "unit", "status": "completed", "conclusion": "failure",
            "html_url": "https://github.com/example/repo/actions/runs/77/job/42",
            "id": 42, "app": {"slug": "github-actions"}}

    def red_check(self, *, green_after_rerun=False, green_after_fix=False,
                  refuse_rerun=False):
        """`UNIT` red on the candidate; green once a rerun was sent (queued
        on the first read after it) or on a later head, as asked."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(refuse_rerun=refuse_rerun)
        self.job_log.write_text("FAIL: test_x (tests.test_y.Case.test_x)")
        heads, reads = [], []
        def check_runs(target, pull, sha):
            heads.extend([sha] if sha not in heads else [])
            if green_after_rerun and self.reruns():
                reads.append(sha)
                return [dict(self.UNIT, status="queued", conclusion=None)
                        if len(reads) == 1 else
                        dict(self.UNIT, conclusion="success")]
            if green_after_fix and sha != heads[0]:
                return [dict(self.UNIT, conclusion="success")]
            return [self.UNIT]
        self.enterContext(patch("holophyte.pr_status._check_runs_of", check_runs))

    def reruns(self):
        return [line for line in self.recorded() if "rerun-failed-jobs" in line]

    def events(self, kind):
        return [summary for (summary,) in self.read(
            f"SELECT summary FROM runEvents WHERE kind = '{kind}' ORDER BY id")]

    def test_a_check_green_on_its_rerun_merges_without_a_fix_turn(self):
        self.red_check(green_after_rerun=True)
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            provider=self.provider())
        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        (rerun,) = self.reruns()
        self.assertIn("--method POST " + RERUN, rerun)
        (event,) = self.events("check_rerun")
        self.assertIn("unit", event)
        self.assertIn("77", event)
        candidate = self.pushed()[-1][1]
        self.assertEqual([v["sha"] for kind, v in self.api_calls()
                          if kind == "merge"], [candidate])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_check_still_red_after_its_rerun_gets_the_fix_turn(self):
        self.red_check(green_after_fix=True)
        task = dict(a_task(), body=self.BODY)
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Commit("fix: the unit failure"), APPROVE, Idle(""),
                            provider=StubProvider(task))
        self.assertEqual(fake.roles, ["implement", "review", "implement"] * 2)
        self.assertIn("CHECK unit", fake.turns[3].goal)
        calls = self.recorded()
        pushes = [n for n, line in enumerate(calls) if line.startswith("git push")]
        (rerun,) = [n for n, line in enumerate(calls) if RERUN in line]
        self.assertLess(pushes[0], rerun)
        self.assertLess(rerun, pushes[1])  # The fix's push follows the rerun.
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_check_red_after_its_rerun_and_fix_parks_without_a_second_rerun(self):
        self.red_check()
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Commit("fix: the unit failure"), provider=self.provider())
        self.assertEqual(fake.roles,
                         ["implement", "review", "implement", "implement"])
        self.assertEqual(len(self.reruns()), 1)
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])
        self.assertIn("checks failure on the head commit", self.question())

    def test_a_refused_rerun_is_warned_and_the_fix_turn_runs(self):
        self.red_check(green_after_fix=True, refuse_rerun=True)
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Commit("fix: the unit failure"), APPROVE, Idle(""),
                            provider=self.provider())
        self.assertEqual(fake.roles, ["implement", "review", "implement"] * 2)
        self.assertIn("CHECK unit", fake.turns[3].goal)
        (warning,) = [w for w in self.events("warning") if "rerun" in w]
        self.assertIn("unit", warning)
        self.assertIn("HTTP 403", warning)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])


if __name__ == "__main__":
    unittest.main()
