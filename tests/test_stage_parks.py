"""The merge gate and each park in `awaiting_merge_approval` as stage
attempts of the real loop: the gate's outcome and edge, the `waiting`
attempt's park kind, and its close from the release that ended the park.

Run: python3 -m unittest discover -s tests -p 'test_stage_parks.py' -v
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import babysit_fixture as cases  # noqa: E402 - after the sys.path insert above
from fake_agent import APPROVE, Commit, Idle  # noqa: E402
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    UNTRIMMED,
    LoopFixture,
    MergeModeFixture,
    StubProvider,
    a_task,
)

import holophyte.loop.review_round  # noqa: E402 - after the sys.path insert
import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import holophyte.stages.record  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.read  # noqa: E402 - after the sys.path insert above
import store.stages  # noqa: E402 - after the sys.path insert above

HUMAN = '[merge]\nmode = "local"\napprove = "human"\n' + UNTRIMMED


class StageQueries:
    def attempts(self, stage, run_id=None):
        where = "" if run_id is None else f" AND runId = {run_id}"
        return self.read(
            "SELECT runId, attempt, outcome, edge, inputSha, outputSha,"
            " endedAt, detail FROM stageAttempts"
            f" WHERE stageId = '{stage}'{where} ORDER BY id")

    def release_at(self, action):
        (at,), = self.read("SELECT at FROM interventions"
                           f" WHERE \"action\" = '{action}'")
        return at

    def released(self, action):
        with store.open(str(self.project.store_path)) as conn:
            ticket = store.read.ticket_by_identifier(conn, "KO-131")
            getattr(store, action)(conn, ticket.id, "go on")


class GateAndApprovalParkTests(StageQueries, LoopFixture):
    def test_an_auto_approved_run_passes_its_gate_once_and_merges(self):
        self.configure('[merge]\nmode = "local"\napprove = "auto"\n'
                       + UNTRIMMED)
        self.loop(Commit("work"), APPROVE)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        candidate = self.git("rev-parse", "main^2").strip()
        self.assertEqual([row[1:6] for row in self.attempts("merge_gate")],
                         [(1, "pass", "next", candidate, candidate)])
        self.assertEqual(self.attempts("waiting"), [])

    def test_a_human_park_waits_as_an_approval_until_approve_releases_it(self):
        self.configure(HUMAN)
        self.loop(Commit("work"), APPROVE)

        gated = self.git("rev-parse", BRANCH).strip()
        (run, attempt, outcome, edge, input_sha, _, ended, detail), = \
            self.attempts("waiting")
        self.assertEqual((run, attempt, outcome, edge, input_sha, ended),
                         (1, 1, None, None, gated, None))
        self.assertEqual(json.loads(detail), {
            "kind": "approval", "approve": "human",
            "reason": f"approved and verified; {BRANCH} at {gated[:12]} waits"
                      ' for a human to say merge ([merge] approve = "human")'})

        self.released("approve")
        self.loop()

        (_, _, outcome, edge, _, _, ended, _), = self.attempts("waiting")
        self.assertEqual((outcome, edge, ended),
                         ("pass", "next", self.release_at("approve")))
        self.assertEqual([row[:4] for row in self.attempts("merge_gate", 2)],
                         [(2, 1, "pass", "next")])
        self.assertEqual(self.read("SELECT id, outcome FROM runs ORDER BY id"),
                         [(1, "abandoned"), (2, "merged")])

    def test_a_before_merge_command_failing_at_the_gate_ends_the_gate(self):
        self.configure('[verify]\nbefore_merge = ["exit 1"]\n' + UNTRIMMED)
        self.loop(Commit("work"), APPROVE)

        self.assertEqual([row[1:4] for row in self.attempts("merge_gate")],
                         [(1, "fail", "end")])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM ledger"
            " WHERE text LIKE 'FAILED verify before merge%'"), [(1,)])


class CheckParkTests(StageQueries, cases.BabysitHelpers, MergeModeFixture):
    UNIT = {"name": "unit", "status": "completed", "conclusion": "failure",
            "html_url": "https://github.com/example/repo/actions/runs/5/job/42",
            "id": 42, "app": {"slug": "github-actions"}}

    def red_on_every_head(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        self.enterContext(patch.object(holophyte.pr.github, "SLEEP"))
        heads, ids = [], iter(range(42, 10**4))

        def check_runs(target, pull, sha):
            if sha not in heads:
                heads.append(sha)
                self.job_log.write_text(
                    f"FAIL: test_{len(heads)} (tests.test_y.Case.test_x)\n")
            return [dict(self.UNIT, id=next(ids))]
        self.enterContext(patch("holophyte.pr.pr_status._check_runs_of",
                                check_runs))

    def test_a_spent_check_park_waits_as_its_kind_and_babysit_fails_it(self):
        self.red_on_every_head()
        self.loop(Commit("work"), APPROVE, Idle(""), Commit("fix: test_1"),
                  Commit("fix: test_2"), provider=self.provider())

        (phase, park_kind), = self.read("SELECT phase, parkKind FROM runs")
        self.assertEqual(phase, "awaiting_merge_approval")
        (_, _, outcome, _, _, _, _, detail), = self.attempts("waiting")
        self.assertIsNone(outcome)
        detail = json.loads(detail)
        self.assertIn("check fix rounds are used up", detail["reason"])
        self.assertEqual((detail["kind"], detail["approve"]),
                         (park_kind, "human"))
        self.assertNotEqual(detail["kind"], "approval")

        self.released("babysit")
        self.loop(Commit("fix: test_3"), Commit("fix: test_4"),
                  provider=self.provider())

        (_, _, outcome, edge, _, _, ended, _) = self.attempts("waiting", 1)[0]
        self.assertEqual((outcome, edge, ended),
                         ("fail", "on_fail", self.release_at("babysit")))


class LostHead:
    """A worktree whose HEAD names a branch that does not exist when its
    round's verify starts; the ticket's verify command points it back."""

    def __init__(self, worktree):
        self.worktree = worktree
        self.real = holophyte.loop.review_round.set_phase

    def __call__(self, conn, run_id, phase, note=None):
        if phase == "verifying":
            subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/gone"],
                           cwd=self.worktree, check=True)
        return self.real(conn, run_id, phase, note)


class UnrecordedRunTests(StageQueries, LoopFixture):
    REFUSED = sqlite3.OperationalError("database is locked")

    def walked(self):
        phases = [summary for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'phase_change'"
            " ORDER BY runId, seq")]
        entries = [(kind, re.sub(r"actual: [\d.]+ min", "actual: N min", text))
                   for kind, text in self.read(
                       "SELECT kind, text FROM ledger ORDER BY id")]
        return phases, entries, self.read("SELECT outcome FROM runs ORDER BY id")

    def parked_then_approved(self):
        self.configure(HUMAN)
        self.loop(Commit("work"), APPROVE)
        self.released("approve")
        self.loop()

    def fresh_fixture(self):
        self.doCleanups()
        self.setUp()

    def failed_writes(self):
        return {json.loads(payload)["write"] for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'stage_record_failed'"
            " AND level = 'detail'")}

    def test_a_store_refusing_stage_writes_parks_and_merges_alike(self):
        self.parked_then_approved()
        recorded = self.walked()
        self.assertEqual(
            self.read("SELECT stageId FROM stageAttempts WHERE stageId IN"
                      " ('merge_gate', 'waiting') ORDER BY id"),
            [("merge_gate",), ("waiting",), ("merge_gate",)])
        self.fresh_fixture()

        with patch.object(store.stages, "open_stage", side_effect=self.REFUSED), \
                patch.object(store.stages, "close_stage",
                             side_effect=self.REFUSED), \
                patch.object(store.stages, "skip_stage",
                             side_effect=self.REFUSED):
            self.parked_then_approved()

        self.assertEqual(self.walked(), recorded)
        self.assertEqual(recorded[2], [("abandoned",), ("merged",)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM stageAttempts"), [(0,)])
        self.assertEqual(self.failed_writes(), {"open_stage", "skip_stage"})

    def lost_head_run(self):
        self.configure(UNTRIMMED)
        task = dict(a_task(), verify=f"git symbolic-ref HEAD refs/heads/{BRANCH}")
        lost = LostHead(self.worktrees / "ko-131-add-a-thing")
        with patch.object(holophyte.loop.review_round, "set_phase", lost):
            self.loop(Commit("work"), APPROVE, provider=StubProvider(task))
        return self.walked()

    def test_a_head_naming_a_missing_ref_at_verify_runs_as_unrecorded(self):
        with patch.object(holophyte.stages.record, "guarded",
                          return_value=holophyte.stages.record.REFUSED):
            unrecorded = self.lost_head_run()
        self.fresh_fixture()

        recorded = self.lost_head_run()

        self.assertEqual(recorded, unrecorded)
        self.assertEqual(recorded[2], [("merged",)])
        (_, _, outcome, _, input_sha, _, _, _), = self.attempts("verify")
        self.assertEqual((outcome, input_sha), ("pass", None))
        self.assertEqual(self.failed_writes(), {"stage_head"})


if __name__ == "__main__":
    unittest.main()
