"""A stale store-mode ticket parked by the loop reaches Backlog on the board
through the host sweep's pass.

The loop runs on the real fixture repository against a store-mode file
board and parks the one ready ticket, whose implementation notes name a
file `main` has never held; with the loop gone, the host sweep's pass
delivers what the park queued. The assertions read what the operator
sees: the board's state and comments, the store's row, the loop starts.

Run: python3 -m unittest discover -s tests -p 'test_witness_stale_park.py' -v
"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import APPROVE, Commit  # noqa: E402
from loop_fixture import LoopFixture  # noqa: E402
from test_claim_freshness import GONE, STALE_BODY  # noqa: E402
from test_store_claim_loop import STORE_MODE, StoreFiles  # noqa: E402

import store  # noqa: E402
from holophyte.config.config_tables import sweep_config  # noqa: E402
from holophyte.host.supervisor import (  # noqa: E402
    fresh_memory,
    reconcile_parked_pull_requests,
)


def never(*args, **kwargs):
    raise AssertionError("the mirror-mode board fallback was asked")


def wall_ms():
    return int(time.time() * 1000)


class StaleParkWitness(LoopFixture):
    def setUp(self):
        super().setUp()
        self.configure(STORE_MODE)
        self.files = self.target.parent / "team-1"
        self.files.mkdir()
        (self.files / "KO-131.md").write_text(STALE_BODY)
        self.board = StoreFiles(self.files)
        self.memory = fresh_memory()
        self.knobs = sweep_config(self.project)

    def run_loop(self):
        with patch("holophyte.review.freshness.critic_admits",
                   lambda *args: True), \
                patch.object(sys, "stdout", io.StringIO()):
            self.loop(Commit("the scripted work"), APPROVE,
                      provider=self.board)

    def sweep_pass(self, now):
        """One host sweep pass at `now`; the loop starts it owed."""
        started = []
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        with patch("holophyte.host.reconcile._reconcile_pull_requests"), \
                patch("holophyte.host.supervisor.linear_budget_low",
                      return_value=False), \
                patch("holophyte.host.supervisor.board_ready", never), \
                patch("holophyte.host.supervisor.start_loop_for",
                      lambda target, conn, owed, *a, **k: started.append(owed)):
            reconcile_parked_pull_requests(
                self.project, conn, now, self.board, io.StringIO(),
                knobs=self.knobs, memory=self.memory)
        return started

    def board_state(self):
        return self.board.states(["KO-131"])["KO-131"]["name"]

    def comments(self):
        path = self.files / "KO-131.comments.md"
        return path.read_text() if path.exists() else ""

    def ticket(self):
        return self.read("SELECT status, pushState FROM tickets"
                         " WHERE linearIdentifier = 'KO-131'")

    def test_the_sweep_moves_a_stale_parked_issue_to_backlog(self):
        self.run_loop()
        first = wall_ms()

        started = self.sweep_pass(first)

        self.assertEqual(self.board_state(), "Backlog")
        self.assertIn(GONE, self.comments())
        self.assertIn("is not on main", self.comments())
        self.assertEqual(self.ticket()[0][0], "needs_spec")
        self.assertEqual(self.read("SELECT COUNT(*) FROM runs"), [(0,)])
        self.assertEqual(started, [])

    def test_a_later_pass_leaves_it_in_backlog_with_no_push_queued(self):
        self.run_loop()
        first = wall_ms()
        self.sweep_pass(first)

        self.sweep_pass(max(wall_ms(), first + self.knobs.board_ask_ms))

        self.assertEqual(self.board_state(), "Backlog")
        self.assertEqual(self.ticket(), [("needs_spec", None)])
