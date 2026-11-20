"""The repairs a pull request takes as stage attempts of the real loop: each
check, covering and capture fix round, each UI capture, and a store that
refuses every stage write without changing the run.

Run: python3 -m unittest discover -s tests -p 'test_stage_pull_request.py' -v
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import babysit_fixture as cases  # noqa: E402 - after the sys.path insert above
import test_babysit_capture_fix as capture_cases  # noqa: E402
from fake_agent import APPROVE, Commit, FakeAgent, Idle, Reply  # noqa: E402
from loop_fixture import MergeModeFixture  # noqa: E402
from test_covering_fix_round import NEW_FINDING, changes  # noqa: E402
from test_stage_recording import USAGE, Metered  # noqa: E402

import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import store.stages  # noqa: E402 - after the sys.path insert above

FAILING = "FAIL: test_a (tests.test_y.Case.test_a)\n"
PINNED_DATES = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}


class PullRequestStages(cases.BabysitHelpers, MergeModeFixture):
    UNIT = {"name": "unit", "status": "completed", "conclusion": "failure",
            "html_url": "https://github.com/example/repo/actions/runs/5/job/42",
            "id": 42, "app": {"slug": "github-actions"}}

    def attempts(self, *stages, run=None):
        marks = ", ".join(f"'{stage}'" for stage in stages)
        where = "" if run is None else f" AND runId = {run}"
        return [(stage, attempt, outcome, edge, sha_in, sha_out,
                 json.loads(detail)) for stage, attempt, outcome, edge, sha_in,
                sha_out, detail in self.read(
                    "SELECT stageId, attempt, outcome, edge, inputSha,"
                    " outputSha, detail FROM stageAttempts"
                    f" WHERE stageId IN ({marks}){where} ORDER BY id")]

    def red_once(self, merge="", reds=1):
        """`unit` red on the first `reds` heads the checks are read at, green
        on every later head; returns the heads in the order they were read."""
        self.configure('[merge]\nmode = "pr"\n' + merge)
        self.fake_route()
        self.enterContext(patch.object(holophyte.pr.github, "SLEEP"))
        heads, ids = [], iter(range(42, 10**4))

        def check_runs(target, pull, sha):
            if sha not in heads:
                heads.append(sha)
                self.job_log.write_text(FAILING)
            if len(heads) > reds:
                return [dict(self.UNIT, conclusion="success")]
            return [dict(self.UNIT, id=next(ids))]
        self.enterContext(patch("holophyte.pr.pr_status._check_runs_of",
                                check_runs))
        return heads

    def check_fixed(self, merge="check_fix_rounds = 2\n"):
        heads = self.red_once(merge)
        fake = Metered(Commit("the scripted work"), APPROVE, Idle(""),
                       Commit("fix: test_a"), APPROVE, Idle(""))
        self.loop(fake=fake, provider=self.provider())
        return fake, heads

    def walked(self):
        phases = [summary for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'phase_change'"
            " ORDER BY seq")]
        entries = [(kind, re.sub(r"actual: [\d.]+ min", "actual: N min", text))
                   for kind, text in self.read(
                       "SELECT kind, text FROM ledger ORDER BY id")]
        return phases, entries, self.read("SELECT outcome FROM runs")


class CheckFixStageTests(PullRequestStages):
    def test_a_check_fixed_in_one_round_records_its_attempt_and_turn_usage(self):
        fake, heads = self.check_fixed()

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        failing, fixed = heads
        (attempt,) = self.attempts("check_fix")
        self.assertEqual(attempt[:6],
                         ("check_fix", 1, "pass", "next", failing, fixed))
        self.assertEqual((attempt[6]["checks"], attempt[6]["round"],
                          attempt[6]["cap"]), (["unit"], 1, 2))
        (turn,) = [n for n, turn in enumerate(fake.turns, 1)
                   if turn.role == "implement" and "CHECK unit" in turn.goal]
        self.assertEqual(self.read(
            "SELECT inputTokens, outputTokens, costUsd FROM stageAttempts"
            " WHERE stageId = 'check_fix'"),
            [(USAGE[turn]["input_tokens"], USAGE[turn]["output_tokens"],
              USAGE[turn]["cost_usd"])])

    def test_a_store_refusing_every_stage_write_leaves_the_run_unchanged(self):
        self.enterContext(patch.dict(os.environ, PINNED_DATES))
        self.check_fixed()
        recorded = self.walked()
        self.assertEqual(len(self.attempts("check_fix")), 1)
        self.doCleanups()
        self.setUp()
        self.enterContext(patch.dict(os.environ, PINNED_DATES))

        refused = sqlite3.OperationalError("database is locked")
        with patch.object(store.stages, "open_stage", side_effect=refused), \
                patch.object(store.stages, "close_stage", side_effect=refused), \
                patch.object(store.stages, "skip_stage", side_effect=refused):
            self.check_fixed()

        self.assertEqual(self.walked(), recorded)
        self.assertEqual(recorded[2], [("merged",)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM stageAttempts"), [(0,)])
        writes = {json.loads(payload)["write"] for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'stage_record_failed'"
            " AND level = 'detail'")}
        self.assertIn("open_stage", writes)


class CoveringFixStageTests(PullRequestStages):
    def test_a_covering_review_asking_for_changes_records_its_fix_round(self):
        self.configure('[merge]\nmode = "pr"\napprove = "auto"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]), self.pr_state()])
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: ADDRESS -- a real crash"),
                            Commit("fix: default load()"), changes(NEW_FINDING),
                            Commit("fix: keep the empty-input default"),
                            APPROVE, Idle(""), provider=self.provider())

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        rejecting, approving = [turn for turn in fake.turns
                                if turn.role == "review"][-2:]
        (attempt,) = self.attempts("covering_fix")
        self.assertEqual(attempt[:6], (
            "covering_fix", 1, "pass", "next", rejecting.candidate_sha,
            approving.candidate_sha))
        self.assertEqual((attempt[6]["round"], attempt[6]["cap"]), (1, 2))


class CaptureStageTests(PullRequestStages):
    broken_spec = capture_cases.CaptureFixRoundTests.broken_spec

    def test_a_failed_capture_its_fix_round_and_the_passing_capture_in_order(self):
        self.broken_spec()
        fix = capture_cases.SpecFix()
        fake = fix.fake = FakeAgent(
            capture_cases.REQUEST_CHANGES,
            Commit("ui fix", path="ui/app.txt"), fix, APPROVE, Idle(""))
        self.loop(fake=fake, provider=self.provider())

        self.assertEqual(self.read("SELECT outcome FROM runs"
                                   " ORDER BY id DESC LIMIT 1"), [("merged",)])
        (run,), = self.read("SELECT MAX(id) FROM runs")
        walked = [row for row in self.attempts("capture", "capture_fix", run=run)
                  if row[2] != "skipped"]
        self.assertEqual([row[:4] for row in walked], [
            ("capture", 2, "fail", "on_fail"), ("capture_fix", 1, "pass", "next"),
            ("capture", 3, "pass", "next")])
        failed, fixed, passed = walked
        self.assertEqual(fixed[4], failed[4])
        self.assertEqual(passed[4], fixed[5])
        self.assertEqual([view.split()[0] for view in failed[6]["missing"]],
                         ["02"])

    def test_unset_ui_paths_skips_the_capture_with_that_reason(self):
        self.red_once(reds=0)
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  provider=self.provider())

        captures = self.attempts("capture")
        self.assertTrue(captures)
        self.assertEqual({(row[2], row[6]["reason"]) for row in captures},
                         {("skipped", "[merge] ui_paths is unset")})


if __name__ == "__main__":
    unittest.main()
