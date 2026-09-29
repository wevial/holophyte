"""The loop's witness step runs a pass for an open story whose ledger lacks
main's tip, in the serial loop and in the pool's scheduler, and the host
sweep starts a loop for such a story when none is live. A held project gets
neither.

Run: python3 -m unittest discover -s tests -p 'test_witness_step.py' -v
"""
from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import APPROVE, Commit, FakeAgent, no_agent_processes  # noqa: E402
from loop_fixture import VALID_BODY, FakePool, LoopFixture  # noqa: E402

import holophyte.loop  # noqa: E402
import holophyte.operator  # noqa: E402
import holophyte.pool  # noqa: E402
import linear_provider  # noqa: E402
import store  # noqa: E402
import store.board  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.config_tables import sweep_config  # noqa: E402
from holophyte.runs import open_store  # noqa: E402
from holophyte.supervisor import (  # noqa: E402
    fresh_memory,
    reconcile_parked_pull_requests,
)
from holophyte.witness import witness_step  # noqa: E402
from provider import board_for  # noqa: E402
from store.stories import approve_story, file_story  # noqa: E402
from tests.test_native_loop import NATIVE, no_linear  # noqa: E402
from tests.test_witness_runner import PASSES, PYTHON  # noqa: E402

W1_FILE = "tests/test_w1.py"
LEDGER = ("SELECT witnessKey, mainSha, verdict, verifier FROM witnessResults"
          " ORDER BY id")


class WitnessStepTests(LoopFixture):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.use(NATIVE)

    def use(self, config):
        self.configure(config)
        self.board = board_for(self.project)
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, self.board.team, self.project.path)

    def approved_story(self, child_column):
        """An approved story whose one child, in `child_column`, completes
        W1 by landing `W1_FILE`; the parent's ticket id."""
        parent, child = (
            self.ticket_id(store.board.file_ticket(
                self.conn, self.project_id, "NAT", VALID_BODY, column=column))
            for column in ("backlog", child_column))
        file_story(self.conn, parent, [{
            "key": "W1", "criterion": "W1 holds", "file": W1_FILE,
            "command": f"{PYTHON} -m unittest tests.test_w1",
            "source": PASSES}], [(child, "completes", ["W1"])])
        (revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?", (parent,)).fetchone()
        approve_story(self.conn, parent, revision, "operator", "go")
        return parent

    def ticket_id(self, identifier):
        return self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?",
            (identifier,)).fetchone()[0]

    def step(self):
        with patch.object(sys, "stdout", io.StringIO()):
            witness_step(self.project, self.conn, self.project_id)

    def tip(self):
        return self.git("rev-parse", "main").strip()

    def sweep(self):
        """One host sweep pass with no loop live; the owed pairs of each
        loop start it made."""
        started = []
        with patch("holophyte.reconcile._reconcile_pull_requests"), \
                patch("holophyte.supervisor.linear_budget_low",
                      return_value=False), \
                patch("holophyte.supervisor.start_loop_for",
                      lambda target, conn, owed, *a, **k: started.append(owed)):
            reconcile_parked_pull_requests(
                self.project, self.conn, int(time.time() * 1000), self.board,
                io.StringIO(), knobs=sweep_config(self.project),
                memory=fresh_memory())
        return started

    def test_the_serial_loop_witnesses_the_childs_merge_before_it_exits(self):
        self.approved_story("ready")
        fake = FakeAgent(Commit("lands w1", path=W1_FILE, body=PASSES),
                         APPROVE)
        out = io.StringIO()
        with no_agent_processes(), patch.object(sys, "stdout", out), \
                patch.object(holophyte.loop, "agent", fake), \
                patch("holophyte.freshness.critic_admits",
                      return_value=True):
            holophyte.operator.main(self.project, self.board)

        self.assertIn("lands w1", self.subjects(), out.getvalue())
        self.assertEqual(self.read(LEDGER), [
            ("W1", self.base, "absent", "loop"),
            ("W1", self.tip(), "green", "loop")], out.getvalue())

    def test_the_pool_runs_a_pending_pass_before_it_returns(self):
        self.use(NATIVE + "[loop]\nworkers = 2\n")
        self.approved_story("backlog")
        pool = FakePool([])
        with patch.object(holophyte.pool, "SPAWN", pool.spawn), \
                patch.object(holophyte.pool, "WAIT", pool.wait), \
                patch.object(sys, "stdout", io.StringIO()):
            holophyte.operator.main(self.project, self.board)

        self.assertEqual(pool.spawned, [])
        self.assertEqual(self.read(LEDGER), [("W1", self.base, "absent", "loop")])

    def test_the_sweep_starts_a_loop_once_for_a_pending_pass(self):
        parent = self.approved_story("backlog")

        self.assertEqual(self.sweep(), [[(parent, None)]])
        self.step()
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.read(LEDGER), [("W1", self.base, "absent", "loop")])

    def test_a_held_project_neither_starts_a_loop_nor_runs_a_pass(self):
        self.approved_story("backlog")
        store.hold(self.conn, self.project_id, "maintenance")

        self.assertEqual(self.sweep(), [])
        self.step()
        self.assertEqual(self.read(LEDGER), [])
