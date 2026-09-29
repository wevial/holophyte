"""A `ci` park across a store-mode loop, the host sweep and the resume.

The loop parks a run whose pull request's checks are pending, a host sweep
pass reads the pull request once its checks finish and sends the run back
to the babysitter, and the next loop resumes it at the merge gate.

Run: python3 -m unittest discover -s tests -p 'test_witness_ci_park.py' -v
"""
from __future__ import annotations

import io
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import APPROVE, Commit, Idle  # noqa: E402
from loop_fixture import VALID_BODY, MergeModeFixture  # noqa: E402
from test_store_claim_loop import STORE_MODE, StoreFiles  # noqa: E402

import holophyte.pr  # noqa: E402
import holophyte.pr_status  # noqa: E402
import store  # noqa: E402
from holophyte.config_tables import sweep_config  # noqa: E402
from holophyte.supervisor import (  # noqa: E402
    fresh_memory,
    reconcile_parked_pull_requests,
)

OLD = "2000-01-01T00:00:00Z"
UNIT = {"name": "unit", "status": "completed", "conclusion": "failure",
        "html_url": "https://github.com/example/repo/actions/runs/5/job/42",
        "id": 42, "app": {"slug": "github-actions"}}


class CiParkWakeTests(MergeModeFixture):
    def setUp(self):
        super().setUp()
        self.configure(STORE_MODE + '[merge]\nmode = "pr"\n')
        files = self.target.parent / "team-1"
        files.mkdir()
        (files / "KO-131.md").write_text(VALID_BODY)
        self.board = StoreFiles(files)
        self.github = self.open_pull("PENDING")
        real = holophyte.pr_status.graphql

        def graphql(target, pull, query, variables):
            if "mergedBy" not in query:
                return real(target, pull, query, variables)
            return {"repository": {"pullRequest": self.github}}
        self.enterContext(patch.object(holophyte.pr_status, "graphql", graphql))
        self.enterContext(patch.object(holophyte.pr, "SLEEP", lambda s: None))

    def open_pull(self, checks, updated_at=OLD):
        """The reconcile's read of the open pull request, no review content."""
        return {"state": "OPEN", "merged": False, "mergeCommit": None,
                "mergedBy": None, "updatedAt": updated_at,
                "reviewThreads": {"totalCount": 0},
                "commits": {"nodes": [{"commit": {
                    "statusCheckRollup": {"state": checks}}}]}}

    def run_loop(self, *script):
        with patch("holophyte.freshness.critic_admits", return_value=True), \
                patch.object(sys, "stdout", io.StringIO()):
            self.loop(*script, provider=self.board)

    def park_on_pending_checks(self):
        self.fake_route(states=[self.pr_state(checks="PENDING")])
        self.run_loop(Commit("the scripted work"), APPROVE, Idle(""))
        self.assertEqual(
            self.read("SELECT r.phase, r.parkKind, r.prUrl, r.prSeenChecks,"
                      " t.activeRunId FROM runs r JOIN tickets t"
                      " ON t.lastRunId = r.id"),
            [("awaiting_merge_approval", "ci", self.URL, "pending", None)])

    def age(self):
        """Move the parked run's clock past `[merge] pr_poll_sec`."""
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE runs SET lastHeartbeat = lastHeartbeat"
                         " - 200000")
        conn.close()

    def sweep(self):
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        with patch("holophyte.supervisor.linear_budget_low",
                   return_value=False), \
                patch("holophyte.supervisor.start_loop_for"):
            reconcile_parked_pull_requests(
                self.project, conn, int(time.time() * 1000), self.board,
                io.StringIO(), knobs=sweep_config(self.project),
                memory=fresh_memory())

    def supervisor_wakes(self):
        return self.read("SELECT COUNT(*) FROM interventions WHERE"
                         " action = 'babysit' AND source = 'supervisor'")[0][0]

    def test_green_checks_wake_the_park_and_the_next_loop_merges_it(self):
        self.park_on_pending_checks()
        self.age()
        self.github = self.open_pull("SUCCESS")
        self.serve(self.pr_state())

        self.sweep()

        self.assertEqual(self.supervisor_wakes(), 1)
        self.run_loop()
        self.assertEqual(
            self.read("SELECT outcome, prUrl FROM runs ORDER BY id DESC"
                      " LIMIT 1"), [("merged", self.URL)])
        self.assertEqual(
            [kind for kind, _ in self.api_calls()].count("merge"), 1)

    def test_red_checks_seen_early_still_wake_the_park_into_a_rerun(self):
        def check_runs(target, pull, sha):
            if any("rerun-failed-jobs" in call for call in self.recorded()):
                return [dict(UNIT, id=43, conclusion="success")]
            return [UNIT] if red else []
        red = []
        self.enterContext(patch("holophyte.pr_status._check_runs_of",
                                check_runs))
        self.park_on_pending_checks()
        red.append(UNIT)
        self.github = self.open_pull("FAILURE")
        self.serve(self.pr_state(checks="FAILURE"), self.pr_state())
        self.sweep()
        self.assertEqual(self.supervisor_wakes(), 0)
        self.age()

        self.sweep()

        self.assertEqual(self.supervisor_wakes(), 1)
        self.run_loop()
        self.assertEqual(
            [call for call in self.recorded() if "rerun-failed-jobs" in call],
            ["gh api --hostname github.com --method POST"
             " repos/example/repo/actions/runs/5/rerun-failed-jobs"])


if __name__ == "__main__":
    import unittest
    unittest.main()
