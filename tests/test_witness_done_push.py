"""A dependency's board pushes across a store-mode loop and the host sweep.

The loop runs KO-131 while a host sweep pass sends its queued In Progress;
the next pass, an ask interval later, sends the merged ticket's Done, and
KO-132, blocked on the board by KO-131 until KO-131 closes, is released and
runs.

Run: python3 -m unittest discover -s tests -p 'test_witness_done_push.py' -v
"""
from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import APPROVE, Commit  # noqa: E402
from loop_fixture import VALID_BODY, LoopFixture  # noqa: E402
from test_store_claim_loop import STORE_MODE, StoreFiles  # noqa: E402

import store  # noqa: E402
from holophyte.config_tables import sweep_config  # noqa: E402
from holophyte.supervisor import (  # noqa: E402
    fresh_memory,
    reconcile_parked_pull_requests,
)
from provider import CLOSED_STATE_NAMES  # noqa: E402


class BlockingFiles(StoreFiles):
    """The store-mode file board on which KO-131 blocks KO-132 until the
    board shows KO-131 closed, as Linear drops a completed blocker."""

    def _blockers(self, task):
        if (task["id"] == "KO-132"
                and self._state("KO-131") not in CLOSED_STATE_NAMES):
            return ["KO-131"]
        return []

    def fetch_task(self, issue_id):
        task = super().fetch_task(issue_id)
        if task is not None:
            task["blocked_by"] = self._blockers(task)
        return task

    def listing(self):
        return [dict(task, blocked_by=self._blockers(task))
                for task in self.ready_issues()]


class SweepThenCommit(Commit):
    """An implementer turn that runs one host sweep pass, then commits."""

    def __init__(self, test, message):
        super().__init__(message)
        self.test = test

    def play(self, cwd, turn):
        self.test.first_pass = self.test.sweep(now_ms())
        return super().play(cwd, turn)


def now_ms():
    return int(time.time() * 1000)


class DonePushReleasesDependentTests(LoopFixture):
    def setUp(self):
        super().setUp()
        self.configure(STORE_MODE)
        files = self.target.parent / "team-1"
        files.mkdir()
        for identifier in ("KO-131", "KO-132"):
            (files / f"{identifier}.md").write_text(VALID_BODY)
        self.board = BlockingFiles(files)
        self.memory = fresh_memory()
        self.knobs = sweep_config(self.project)

    def sweep(self, now):
        """One host sweep pass at `now`; the loop start it owed, if any."""
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        started = []
        with patch("holophyte.reconcile._reconcile_pull_requests"), \
                patch("holophyte.supervisor.linear_budget_low",
                      return_value=False), \
                patch("holophyte.supervisor.start_loop_for",
                      lambda target, conn, owed, *a, **k: started.append(owed)):
            reconcile_parked_pull_requests(
                self.project, conn, now, self.board, io.StringIO(),
                knobs=self.knobs, memory=self.memory)
        return started[0] if started else None

    def run_loop(self, implementer):
        with patch("holophyte.freshness.critic_admits", return_value=True), \
                patch.object(sys, "stdout", io.StringIO()):
            self.loop(implementer, APPROVE, provider=self.board)

    def ticket(self, identifier):
        ((ticket_id, status),) = self.read(
            "SELECT id, status FROM tickets"
            f" WHERE linearIdentifier = '{identifier}'")
        return ticket_id, status

    def outcomes(self, identifier):
        return [outcome for (outcome,) in self.read(
            "SELECT r.outcome FROM runs r JOIN tickets t ON t.id = r.ticketId"
            f" WHERE t.linearIdentifier = '{identifier}'")]

    def test_the_done_push_releases_the_dependent_which_then_runs(self):
        self.run_loop(SweepThenCommit(self, "the blocker's work"))

        self.assertIsNone(self.first_pass)
        self.assertEqual(self.board.states(["KO-131"])["KO-131"]["name"],
                         "In Progress")
        self.assertEqual(self.outcomes("KO-131"), ["merged"])
        self.assertEqual(self.ticket("KO-132")[1], "blocked_on_deps")
        self.assertEqual(self.outcomes("KO-132"), [])
        ((depends,),) = self.read("SELECT dependsOn FROM tickets"
                                  " WHERE linearIdentifier = 'KO-132'")
        self.assertEqual(json.loads(depends), ["KO-131"])

        owed = self.sweep(now_ms() + self.knobs.board_ask_ms)

        self.assertEqual(self.board.states(["KO-131"])["KO-131"]["name"],
                         "Done")
        dependent, status = self.ticket("KO-132")
        self.assertEqual(status, "ready")
        self.assertEqual(owed, [(dependent, None)])

        self.run_loop(Commit("the dependent's work"))

        self.assertEqual(self.outcomes("KO-132"), ["merged"])
        self.assertIn("the dependent's work", self.subjects())


if __name__ == "__main__":
    import unittest
    unittest.main()
