"""KO-707: a red Actions check's failed jobs are rerun once before the fix turn."""
from __future__ import annotations

import io
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # factory.py imports store by name
sys.path.insert(0, str(HERE))  # `discover -s tests` and `-m unittest` alike
import babysit_fixture as cases  # noqa: E402
from fake_agent import APPROVE, Commit, Idle  # noqa: E402
from loop_fixture import UNTRIMMED, MergeModeFixture, StubProvider, a_task  # noqa: E402

import holophyte.cli.operator  # noqa: E402 - after the sys.path insert above
import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pr_status  # noqa: E402 - after the sys.path insert above

RERUN = "repos/example/repo/actions/runs/77/rerun-failed-jobs"
JOB = "https://github.com/example/repo/actions/runs/77/job/"


TEST_Y = "tests/test_y.py"
CASE = ("import time\nimport unittest\n\n\n"
        "class Case(unittest.TestCase):\n    def test_x(self):\n        {}\n")
FAILING = CASE.format("self.assertTrue(1 == 2)")
PASSING = CASE.format("self.assertTrue(1 == 1)")
SLEEPING = CASE.format("time.sleep(30)")
SHARD_LOG = "2024-05-01T10:00:00.1Z FAIL: test_x (test_y.Case.test_x)"


class BabysitRerunTests(cases.BabysitHelpers, MergeModeFixture):
    """A flake goes green on its rerun and keeps the fix turn unspent."""
    UNIT = {"name": "unit", "status": "completed", "conclusion": "failure",
            "html_url": JOB + "42", "id": 42, "app": {"slug": "github-actions"}}
    # GitHub runs a rerun's jobs as new jobs of the same workflow run.
    RERUN_RED = dict(UNIT, html_url=JOB + "43", id=43)
    QUEUED = dict(RERUN_RED, status="queued", conclusion=None)
    GREEN = dict(RERUN_RED, conclusion="success")
    UNLISTED = None  # A starting rerun: GitHub lists no check run on the head.

    def red_check(self, *after_rerun, green_after_fix=False, refuse_rerun=False,
                  config="", rollup="SUCCESS",
                  log="FAIL: test_x (tests.test_y.Case.test_x)"):
        """`UNIT` red on the candidate; once a rerun was sent, the rows of
        `after_rerun` read in order, the last for good (the rerun's own red
        job without them); green on a later head when `green_after_fix`.
        `rollup` is the head's status rollup state, None for none."""
        self.configure('[merge]\nmode = "pr"\n' + config + UNTRIMMED)
        self.fake_route(refuse_rerun=refuse_rerun,
                        states=[self.pr_state(checks=rollup)])
        self.job_log.write_text(log)
        self.naps = []
        self.enterContext(patch.object(holophyte.pr.github, "SLEEP", self.naps.append))
        heads, served = [], list(after_rerun or [self.RERUN_RED])
        def check_runs(target, pull, sha):
            heads.extend([sha] if sha not in heads else [])
            if green_after_fix and sha != heads[0]:
                return [self.GREEN]
            if not self.reruns():
                return [self.UNIT]
            row = served.pop(0) if len(served) > 1 else served[0]
            return [] if row is self.UNLISTED else [row]
        self.enterContext(patch("holophyte.pr.pr_status._check_runs_of", check_runs))

    def reruns(self):
        return [line for line in self.recorded() if "rerun-failed-jobs" in line]

    def events(self, kind):
        return [summary for (summary,) in self.read(
            f"SELECT summary FROM runEvents WHERE kind = '{kind}' ORDER BY id")]

    def assert_merged_without_a_fix_turn(self, fake):
        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        candidate = self.pushed()[-1][1]
        self.assertEqual([v["sha"] for kind, v in self.api_calls()
                          if kind == "merge"], [candidate])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_check_green_on_its_rerun_merges_without_a_fix_turn(self):
        self.red_check(self.QUEUED, self.GREEN)
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            provider=self.provider())
        (rerun,) = self.reruns()
        self.assertIn("--method POST " + RERUN, rerun)
        (event,) = self.events("check_rerun")
        self.assertIn("unit", event)
        self.assertIn("77", event)
        self.assert_merged_without_a_fix_turn(fake)

    def test_a_red_read_from_before_the_rerun_is_read_again(self):
        # Review finding: the first read after the POST still showed the
        # rerun job's old failure, and the fix turn was spent on it.
        self.red_check(self.UNIT, self.QUEUED, self.GREEN)
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            provider=self.provider())
        self.assertEqual(len(self.reruns()), 1)
        self.assert_merged_without_a_fix_turn(fake)

    def test_a_rerun_that_never_reports_parks_at_the_check_wait_deadline(self):
        # Review finding: a fixed nap before the settle overran the deadline.
        self.red_check(self.UNIT, config="check_wait_sec = 60\n")
        clock = {"side_effect": lambda: sum(self.naps)}
        with patch("holophyte.babysit.babysitter.monotonic", **clock), \
                patch("holophyte.babysit.check_fix.monotonic", **clock):
            fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                                provider=self.provider())
        self.assertEqual(sum(self.naps), 60)
        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        self.assertIn("the rerun of unit did not report within 60s",
                      self.question())

    def loop_on_a_nap_clock(self):
        clock = {"side_effect": lambda: sum(self.naps)}
        with patch("holophyte.babysit.babysitter.monotonic", **clock), \
                patch("holophyte.babysit.check_fix.monotonic", **clock):
            fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                                provider=self.provider())
        return fake

    def test_a_rerun_never_listed_reads_pending_and_parks_unmerged(self):
        self.red_check(self.UNLISTED, rollup=None,
                       config="check_wait_sec = 60\nmissing_check_sec = 30\n")
        folded, real = [], holophyte.pr.pr_status.pr_state
        def pr_state(target, pull):
            state = real(target, pull)
            folded.extend([state.checks] if self.reruns() else [])
            return state
        self.enterContext(patch.object(holophyte.pr.pr_status, "pr_state", pr_state))
        self.loop_on_a_nap_clock()
        self.assertEqual(len(self.reruns()), 1)
        self.assertEqual(folded, ["pending"] * 3)
        self.assertEqual(sum(self.naps), 60)
        self.assertFalse([v for kind, v in self.api_calls() if kind == "merge"])
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])
        self.assertIn("the rerun of unit did not report within 60s",
                      self.question())

    def test_a_rerun_listed_red_after_an_unlisted_read_gets_the_fix_turn(self):
        self.red_check(self.UNLISTED, self.RERUN_RED, rollup=None,
                       green_after_fix=True)
        task = dict(a_task(), body=self.BODY)
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Commit("fix: the unit failure"), APPROVE, Idle(""),
                            provider=StubProvider(task))
        self.assertEqual(fake.roles, ["implement", "review", "implement"] * 2)
        self.assertIn("CHECK unit", fake.turns[3].goal)
        _, fixed = (sha for _, sha in self.pushed())
        self.assertEqual([v["sha"] for kind, v in self.api_calls()
                          if kind == "merge"], [fixed])

    def test_a_rerun_listed_green_after_missing_check_sec_unlisted_merges(self):
        self.red_check(self.UNLISTED, self.UNLISTED, self.GREEN, rollup=None,
                       config="check_wait_sec = 120\nmissing_check_sec = 30\n")
        fake = self.loop_on_a_nap_clock()
        self.assertEqual(sum(self.naps), 60)
        self.assert_merged_without_a_fix_turn(fake)

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

    def test_an_approval_rerun_to_green_keeps_its_sha_on_a_quiet_wait_park(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       'check_wait_sec = 60\n')
        self.fake_route(states=[self.pr_state()])
        self.naps, red = [], []
        self.enterContext(patch.object(holophyte.pr.github, "SLEEP", self.naps.append))
        self.enterContext(patch(
            "holophyte.pr.pr_status._check_runs_of", lambda *_: [
                self.UNIT if red and not self.reruns() else self.GREEN]))
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  provider=self.provider())
        self.assertIn("waiting for a human to say merge", self.question())
        approved = self.pushed()[-1][1]
        self.serve(self.pr_state(head=approved, review="APPROVED"))
        holophyte.cli.operator.approve(self.project, "KO-131", "merge it",
                                       out=io.StringIO())
        red.append(True)
        self.serve(self.pr_state(
            updated_at=datetime.now(timezone.utc).isoformat()))
        clock = {"side_effect": lambda: sum(self.naps)}
        with patch("holophyte.babysit.babysitter.monotonic", **clock), \
                patch("holophyte.babysit.check_fix.monotonic", **clock):
            self.loop(provider=self.provider())
        self.assertEqual(len(self.reruns()), 1)
        self.assertIn("quiet wait exceeded 60s", self.question())
        self.assertEqual(self.read("SELECT payload FROM runEvents WHERE runId = 2"
                                   " AND kind = 'approval_parked'"),
                         [(f'{{"sha": "{approved}"}}',)])

    def rerun_summary(self):
        (summary,) = self.events("check_rerun")
        return summary

    def test_a_failure_reproduced_locally_skips_the_rerun_for_the_fix_turn(self):
        self.red_check(green_after_fix=True, log=SHARD_LOG)
        fake, _ = self.loop(Commit("a failing test", path=TEST_Y, body=FAILING),
                            APPROVE, Idle(""),
                            Commit("fix: the unit failure"), APPROVE, Idle(""),
                            provider=self.provider())
        self.assertEqual(self.reruns(), [])
        ((skip_id, summary, payload),) = self.read(
            "SELECT id, summary, payload FROM runEvents"
            " WHERE kind = 'check_rerun_skipped'")
        self.assertIn("test_y.Case.test_x", summary)
        self.assertIn("AssertionError", json.loads(payload)["output"])
        ((round_id,),) = self.read(
            "SELECT id FROM runEvents WHERE kind = 'check_fix_round'")
        self.assertLess(skip_id, round_id)
        self.assertEqual(fake.roles, ["implement", "review", "implement"] * 2)
        self.assertIn("- FAIL: test_x (test_y.Case.test_x)", fake.turns[3].goal)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_reproduced_failure_red_again_after_its_fix_parks_with_no_rerun(self):
        self.red_check(log=SHARD_LOG)
        fake, _ = self.loop(Commit("a failing test", path=TEST_Y, body=FAILING),
                            APPROVE, Idle(""), Commit("fix: the unit failure"),
                            provider=self.provider())
        self.assertEqual(fake.roles,
                         ["implement", "review", "implement", "implement"])
        self.assertEqual(self.reruns(), [])
        self.assertEqual(len(self.events("check_rerun_skipped")), 1)
        self.assertIn("checks failure on the head commit", self.question())

    def test_a_failure_passing_locally_is_rerun_and_merges_green(self):
        self.red_check(self.QUEUED, self.GREEN, log=SHARD_LOG)
        fake, _ = self.loop(Commit("a passing test", path=TEST_Y, body=PASSING),
                            APPROVE, Idle(""), provider=self.provider())
        self.assertEqual(len(self.reruns()), 1)
        candidate = self.pushed()[-1][1]
        self.assertTrue(self.rerun_summary().endswith(
            f"; local run: passed at {candidate[:12]}"))
        self.assertEqual(self.events("check_rerun_skipped"), [])
        self.assert_merged_without_a_fix_turn(fake)

    def test_a_job_log_with_no_unittest_id_is_rerun(self):
        self.red_check(self.QUEUED, self.GREEN,
                       log="python did not pass: failure")
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            provider=self.provider())
        self.assertEqual(len(self.reruns()), 1)
        self.assertTrue(self.rerun_summary().endswith(
            "; local run: no unittest test id in the failed job logs"))
        self.assert_merged_without_a_fix_turn(fake)

    def test_a_named_module_missing_from_the_worktree_is_rerun(self):
        self.red_check(self.QUEUED, self.GREEN)
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  provider=self.provider())
        self.assertEqual(len(self.reruns()), 1)
        self.assertTrue(self.rerun_summary().endswith(
            "; local run: module tests.test_y not found in the worktree"))

    def test_a_local_run_over_its_cap_is_stopped_and_rerun(self):
        self.red_check(self.QUEUED, self.GREEN, log=SHARD_LOG)
        started = monotonic()
        with patch("holophyte.babysit.check_fix.LOCAL_RUN_SEC", 2):
            fake, _ = self.loop(
                Commit("a slow test", path=TEST_Y, body=SLEEPING), APPROVE,
                Idle(""), provider=self.provider())
        self.assertLess(monotonic() - started, 30)
        self.assertEqual(len(self.reruns()), 1)
        self.assertTrue(self.rerun_summary().endswith(
            "; local run: timed out after 2 s"))
        self.assert_merged_without_a_fix_turn(fake)


    def test_a_class_fixture_error_beside_a_reproduced_failure_skips_the_rerun(self):
        self.red_check(log="ERROR: setUpClass (checks.test_sample.SetupBroken)\n"
                       + SHARD_LOG)
        self.loop(Commit("a failing test", path=TEST_Y, body=FAILING),
                  APPROVE, Idle(""), Commit("fix: the unit failure"),
                  provider=self.provider())
        self.assertEqual(self.reruns(), [])
        ((payload,),) = self.read("SELECT payload FROM runEvents"
                                  " WHERE kind = 'check_rerun_skipped'")
        self.assertEqual(json.loads(payload)["ids"], ["test_y.Case.test_x"])

    def test_a_failure_is_reproduced_only_for_the_module_that_ran_it(self):
        for package in ("first", "second"):
            (self.target / package).mkdir()
            (self.target / package / "test_shared.py").write_text(PASSING)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "two passing shared tests")
        self.red_check(log="FAIL: test_x (first.test_shared.Case.test_x)\n"
                       "FAIL: test_x (second.test_shared.Case.test_x)")
        self.loop(Commit("a failing test", path="first/test_shared.py",
                         body=FAILING),
                  APPROVE, Idle(""), Commit("fix: the unit failure"),
                  provider=self.provider())
        self.assertEqual(self.reruns(), [])
        ((payload,),) = self.read("SELECT payload FROM runEvents"
                                  " WHERE kind = 'check_rerun_skipped'")
        self.assertEqual(json.loads(payload)["reproduced"],
                         ["first.test_shared.Case.test_x"])


if __name__ == "__main__":
    unittest.main()
