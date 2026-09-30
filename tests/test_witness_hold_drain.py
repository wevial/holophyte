"""The runbook's hold and drain before a move to the native board: a hold
during a live run, the run finishing, the host sweep's passes delivering
and settling what the run queued, then a dry-run board import that counts
no push and no note pending for Linear.

Run: python3 -m unittest discover -s tests -p 'test_witness_hold_drain.py' -v
"""
from __future__ import annotations

import contextlib
import io
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import APPROVE, Commit  # noqa: E402
from loop_fixture import VALID_BODY, LoopFixture  # noqa: E402
from test_provider import FakeLinear  # noqa: E402
from test_store_claim_loop import STORE_MODE, StoreFiles  # noqa: E402

import holophyte.cli.entry  # noqa: E402
import linear_provider  # noqa: E402
from holophyte.config.config_tables import sweep_config  # noqa: E402
from holophyte.host.supervisor import (  # noqa: E402
    fresh_memory,
    reconcile_parked_pull_requests,
)
from holophyte.loop.runs import open_store  # noqa: E402

HOLD_NOTE = "moving to the native board"


class HoldThenCommit(Commit):
    """An implementer turn that holds the project through the command line,
    then commits."""

    def __init__(self, test, message):
        super().__init__(message)
        self.test = test

    def play(self, cwd, turn):
        self.test.held_during_turn = self.test.cli("--hold", "--note",
                                                   HOLD_NOTE)
        return super().play(cwd, turn)


class HoldDrainWitness(LoopFixture):
    def setUp(self):
        super().setUp()
        self.configure(STORE_MODE)
        self.files = self.target.parent / "team-1"
        self.files.mkdir()
        for identifier in ("KO-131", "KO-132"):
            (self.files / f"{identifier}.md").write_text(VALID_BODY)
        self.board = StoreFiles(self.files)
        self.memory = fresh_memory()
        self.held_during_turn = None

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            holophyte.cli.entry.cli([str(self.target), *args])
        return out.getvalue()

    def drain(self):
        """Run the loop with the hold landing in KO-131's implementer turn;
        what the loop printed."""
        out = io.StringIO()
        with patch("holophyte.review.freshness.critic_admits",
                   lambda *a, **k: True), patch.object(sys, "stdout", out):
            self.loop(HoldThenCommit(self, "the held work"), APPROVE,
                      provider=self.board)
        return out.getvalue()

    def sweep_pass(self):
        """One host sweep pass at the wall clock with no loop live; the
        pairs it owed a loop start, or None when it started none."""
        started = []
        conn = open_store(self.project)
        self.addCleanup(conn.close)
        with patch("holophyte.host.reconcile._reconcile_pull_requests"), \
                patch("holophyte.host.supervisor.linear_budget_low",
                      return_value=False), \
                patch("holophyte.host.supervisor.start_loop_for",
                      lambda target, conn, owed, *a, **k: started.append(owed)):
            reconcile_parked_pull_requests(
                self.project, conn, int(time.time() * 1000), self.board,
                io.StringIO(), knobs=sweep_config(self.project),
                memory=self.memory)
        return started[0] if started else None

    def dry_run_summary(self):
        """`--board-import --dry-run` against a Linear board holding KO-131
        Done and KO-132 Todo; its summary line, after checking it wrote
        nothing."""
        linear = FakeLinear()
        linear.add("KO-131", "add a thing", VALID_BODY, state="Done")
        linear.add("KO-132", "add a thing", VALID_BODY)
        with patch.object(linear_provider, "_gql", linear.gql):
            out = self.cli("--board-import", "--dry-run")
        self.assertIn("dry run: nothing written", out)
        (summary,) = [line for line in out.splitlines()
                      if line.startswith("[holo2] board import: ")]
        return summary

    def board_state(self, identifier):
        state = self.files / f"{identifier}.state"
        return state.read_text().strip() if state.exists() else None

    def posted_notes(self, identifier):
        comments = self.files / f"{identifier}.comments.md"
        text = comments.read_text() if comments.exists() else ""
        return sum(line.startswith("holophyte-note: ")
                   for line in text.splitlines())

    def recorded_notes(self, identifier):
        return self.read(
            "SELECT COUNT(*) FROM ticketNotes n JOIN tickets t"
            " ON t.id = n.ticketId WHERE t.linearIdentifier = "
            f"'{identifier}'")[0][0]

    def test_a_hold_mid_run_lets_the_run_merge_and_claims_nothing_more(self):
        out = self.drain()

        self.assertIn(f"held: {HOLD_NOTE}", self.held_during_turn)
        self.assertIn(f"held: {HOLD_NOTE}", out)
        self.assertIn("the held work", self.subjects())
        self.assertEqual(self.read(
            "SELECT t.linearIdentifier, t.status FROM runs r"
            " JOIN tickets t ON t.id = r.ticketId"), [("KO-131", "merged")])
        self.assertEqual(self.read(
            "SELECT status, activeRunId FROM tickets"
            " WHERE linearIdentifier = 'KO-132'"), [("ready", None)])

    def test_one_sweep_pass_delivers_the_held_projects_push_and_notes(self):
        self.drain()

        self.assertIsNone(self.sweep_pass())

        self.assertEqual(self.board_state("KO-131"), "Done")
        self.assertGreater(self.recorded_notes("KO-131"), 0)
        self.assertEqual(self.posted_notes("KO-131"),
                         self.recorded_notes("KO-131"))
        self.assertEqual(self.read("SELECT admission FROM projects"),
                         [("held",)])

    def test_the_first_pass_leaves_the_sent_push_pending_until_it_settles(self):
        self.drain()
        self.sweep_pass()

        summary = self.dry_run_summary()

        self.assertTrue(
            summary.endswith("; 1 pushes and 0 notes pending for Linear"),
            summary)

    def test_the_dry_run_then_counts_nothing_pending_for_linear(self):
        self.drain()
        self.sweep_pass()
        self.assertIsNone(self.sweep_pass())

        summary = self.dry_run_summary()

        self.assertTrue(
            summary.endswith("; 0 pushes and 0 notes pending for Linear"),
            summary)
