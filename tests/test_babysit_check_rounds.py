"""A check red with a new failure after its fix gets another check fix round."""
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
from loop_fixture import BRANCH, MergeModeFixture, StubProvider, a_task  # noqa: E402

import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
from holophyte.babysit.check_failures import Failure  # noqa: E402


class BabysitCheckRoundsTests(cases.BabysitHelpers, MergeModeFixture):
    UNIT = {"name": "unit", "status": "completed", "conclusion": "failure",
            "html_url": "https://github.com/example/repo/actions/runs/5/job/42",
            "id": 42, "app": {"slug": "github-actions"}}

    def red_per_head(self, *logs, merge=""):
        """`UNIT` red on the n-th head with `logs[n]` as its job log, a new
        job id on every read; green on every head past the last log."""
        self.configure('[merge]\nmode = "pr"\n' + merge)
        self.fake_route()
        self.enterContext(patch.object(holophyte.pr.github, "SLEEP"))
        heads, ids = [], iter(range(42, 10**4))
        def check_runs(target, pull, sha):
            if sha not in heads:
                heads.append(sha)
                if len(heads) <= len(logs):
                    self.job_log.write_text(logs[len(heads) - 1])
            if len(heads) > len(logs):
                return [dict(self.UNIT, conclusion="success")]
            return [dict(self.UNIT, id=next(ids))]
        self.enterContext(patch("holophyte.pr.pr_status._check_runs_of", check_runs))

    def events(self, kind):
        return [summary for (summary,) in self.read(
            f"SELECT summary FROM runEvents WHERE kind = '{kind}' ORDER BY id")]

    def test_a_new_failure_on_the_fix_commit_gets_a_second_round_and_merges(self):
        self.red_per_head("FAIL: test_a (tests.test_y.Case.test_a)\n",
                          "FAIL: test_b (tests.test_y.Case.test_b)\n")
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Commit("fix: test_a"), Commit("fix: test_b"),
                            APPROVE, Idle(""),
                            provider=StubProvider(dict(a_task(), body=self.BODY)))
        fixes = [turn for turn in fake.turns[3:] if turn.role == "implement"
                 and "CHECK unit" in turn.goal]
        self.assertEqual(len(fixes), 2, self.read("SELECT outcomeReason FROM runs"))
        self.assertIn("FAIL: test_b", fixes[1].goal)
        self.assertIn("check fix round 2 of 2", fixes[1].goal)
        fixed = self.pushed()[-1][1]
        self.assertIn("fix: test_b", self.git("log", "-1", "--format=%s", fixed))
        self.assertEqual([v["sha"] for kind, v in self.api_calls()
                          if kind == "merge"], [fixed])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual(len(self.events("check_fix_round")), 2)
        self.assertIn("Check fix round 2 of 2", " ".join(
            text for (text,) in self.read(
                "SELECT text FROM ledger WHERE kind = 'round'")))

    def fix_goals(self, fake):
        return [turn.goal for turn in fake.turns[3:] if turn.role == "implement"
                and "CHECK unit" in turn.goal]

    def merges(self):
        return [v["sha"] for kind, v in self.api_calls() if kind == "merge"]

    def test_the_same_failure_with_new_stamps_and_digests_parks_as_a_repeat(self):
        self.red_per_head(
            "2026-10-15T10:00:01.1234567Z FAIL: test_a (tests.test_y.Case.test_a)\n"
            "2026-10-15T10:00:01.2234567Z AssertionError: digest"
            " 0123456789abcdef != expected (1.25s)\n",
            "2026-10-15T11:42:09.7654321Z FAIL: test_a (tests.test_y.Case.test_a)\n"
            "2026-10-15T11:42:09.8654321Z AssertionError: digest"
            " fedcba9876543210 != expected (3.5s)\n")
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Commit("fix: test_a"), Commit("fix: again"),
                            provider=self.provider())
        self.assertEqual(len(self.fix_goals(fake)), 1)
        self.assertEqual(self.read("SELECT phase FROM runs"),
                         [("awaiting_merge_approval",)])
        question = self.question()
        self.assertIn("checks failure on the head commit", question)
        self.assertIn("the failure check fix round 1 was handed repeats", question)
        self.assertEqual(self.merges(), [])

    def test_one_round_parks_a_new_failure_as_rounds_used_up(self):
        self.red_per_head("FAIL: test_a (tests.test_y.Case.test_a)\n",
                          "FAIL: test_b (tests.test_y.Case.test_b)\n",
                          merge="check_fix_rounds = 1\n")
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Commit("fix: test_a"), Commit("fix: test_b"),
                            provider=self.provider())
        self.assertEqual(len(self.fix_goals(fake)), 1)
        self.assertIn("a new failure, and the check fix rounds are used up"
                      " ([merge] check_fix_rounds = 1)", self.question())

    def test_a_new_failure_on_each_of_three_heads_parks_after_two_rounds(self):
        self.red_per_head(*(f"FAIL: test_{n} (tests.test_y.Case.test_{n})\n"
                            for n in "abc"))
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Commit("fix: test_a"), Commit("fix: test_b"),
                            Commit("fix: test_c"), provider=self.provider())
        self.assertEqual(len(self.fix_goals(fake)), 2)
        self.assertIn("check fix rounds are used up ([merge] check_fix_rounds = 2)",
                      self.question())
        self.assertEqual(self.merges(), [])

    def test_the_brief_lists_every_failure_and_ends_its_tail_at_the_error(self):
        stamp = "2026-10-15T10:00:00.0000000Z "
        fails = [f"FAIL: test_{n} (tests.test_y.Case.test_{n})" for n in range(3)]
        log = "".join(f"{stamp}{line}\n" for line in (
            fails + [f"  traceback frame {n}" for n in range(120)]
            + ["##[error]Process completed with exit code 1."]
            + [f"Post job {n}: Cleaning up orphan processes" for n in range(30)]))
        self.red_per_head(log)
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Commit("fix: the tests"), APPROVE, Idle(""),
                            provider=self.provider())
        (goal,) = self.fix_goals(fake)
        listed = goal.split("Failing tests and errors")[1].split("CHECK unit")[0]
        for line in fails:
            self.assertIn(f"- {line}", listed)
        tail = goal.split("CHECK unit")[1]
        self.assertNotIn("traceback frame 0\n", tail)
        self.assertIn("exit code 1.\n\nFix the failures", tail)
        self.assertNotIn("Cleaning up orphan processes", goal)

    def test_a_human_approved_project_parks_at_the_second_fix_reviewed(self):
        self.red_per_head("FAIL: test_a (tests.test_y.Case.test_a)\n",
                          "FAIL: test_b (tests.test_y.Case.test_b)\n",
                          merge='approve = "human"\nreview_fixes = true\n')
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Commit("fix: test_a"), Commit("fix: test_b"),
                            APPROVE, Idle(""), provider=self.provider())
        self.assertEqual(len(self.fix_goals(fake)), 2)
        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertIn("fix: test_b", self.git("log", "-1", "--format=%s", fixed))
        self.assertIn("waiting for a human to say merge", self.question())
        self.assertEqual(self.read("SELECT phase, approvedSha FROM runs"),
                         [("awaiting_merge_approval", fixed)])
        self.assertEqual(self.merges(), [])


class PlaywrightRepeatTests(unittest.TestCase):
    HEADER = ("  1) [chromium] › e2e/wizard.spec.ts:41:3 › wizard › flag on"
              " extracts the contract ──────")

    def log(self, error, stamp="2026-10-15T10:00:00.0000000Z", retry=""):
        return (f"{stamp} {self.HEADER}{retry}\n{stamp} \n"
                f"{stamp}     {error}\n{stamp}   1 failed (1.2m)\n")

    def verdict(self, earlier, later, names=("e2e (4/4)", "e2e (2/4)")):
        handed = [Failure.read([names[0]], [earlier])]
        return Failure.read([names[1]], [later]).repeated_round(handed)

    def test_the_same_test_failing_with_another_error_is_new(self):
        self.assertIsNone(self.verdict(
            self.log("TestApiError: POST /api/test/fixture -> 400"),
            self.log("Error: locator.click: Test ended.")))

    def test_the_same_log_with_new_stamps_durations_and_retries_repeats(self):
        error = "Error: locator.click: Test ended. (31.4s)"
        self.assertEqual(self.verdict(
            self.log(error),
            self.log(error.replace("31.4s", "29.9s"),
                     stamp="2026-10-15T12:13:14.5555555Z",
                     retry=" (retry #1)")), 1)


if __name__ == "__main__":
    unittest.main()
