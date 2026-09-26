"""KO-762: a native project runs end to end with no Linear call.

The loop runs on the real fixture repository with real git against a
`[board] kind = "native"` project whose tickets are filed through
`store.board`: `NAT-2`, which depends on `NAT-1`, waits at
`blocked_on_deps` until `NAT-1` merges, then runs, with `LINEAR_API_KEY`
unset and Linear's transport failing the test. An unmerged dependency on
a native board is a wait, not a stale body, and the host sweep ends a
finished wait without listing the board.

Run: python3 -m unittest discover -s tests -p 'test_native_loop.py' -v
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import APPROVE, Commit, FakeAgent, no_agent_processes  # noqa: E402
from loop_fixture import VALID_BODY, LoopFixture  # noqa: E402

import holophyte.loop  # noqa: E402
import holophyte.operator  # noqa: E402
import linear_provider  # noqa: E402
import store.board  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.board_sync import owed  # noqa: E402
from holophyte.config_tables import sweep_config  # noqa: E402
from holophyte.freshness import stale_reasons  # noqa: E402
from holophyte.native_board import NativeBoard  # noqa: E402
from holophyte.runs import open_store  # noqa: E402
from provider import FileProvider, board_for  # noqa: E402

NATIVE = '[board]\nkind = "native"\nkey = "NAT"\n'
DEPENDENT = VALID_BODY.replace("Depends on: none", "Depends on: NAT-1")


def no_linear(*args, **kwargs):
    raise AssertionError("a native project asked Linear")


class UnlistedBoard(NativeBoard):
    """The native board, failing the test when it is listed."""

    def listing(self):
        raise AssertionError("a native board was listed")

    ready_issues = listing


class Snapshot(Commit):
    """An implementer commit that first records both tickets' statuses."""

    def __init__(self, test, message):
        super().__init__(message)
        self.test = test

    def play(self, cwd, turn):
        self.test.seen.append(self.test.statuses())
        return super().play(cwd, turn)


class NativeLoopTests(LoopFixture):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.configure(NATIVE)
        self.board = board_for(self.project)
        self.assertIsInstance(self.board, NativeBoard)
        conn = open_store(self.project)
        self.addCleanup(conn.close)
        self.conn = conn
        self.project_id = store.tickets.ensure_project(
            conn, self.board.team, self.project.path)
        self.seen = []

    def file(self, body):
        return store.board.file_ticket(self.conn, self.project_id, "NAT",
                                       body, column="ready")

    def statuses(self):
        return dict(self.read("SELECT linearIdentifier, status FROM tickets"))

    def run_loop(self):
        fake = FakeAgent(Snapshot(self, "the first work"), APPROVE,
                         Snapshot(self, "the second work"), APPROVE)
        out = io.StringIO()
        with no_agent_processes(), patch.object(sys, "stdout", out), \
                patch.object(holophyte.loop, "agent", fake), \
                patch("holophyte.freshness.critic_admits",
                      return_value=True):
            self.rc = holophyte.operator.main(self.project, self.board)
        return out.getvalue()

    def test_a_dependent_ticket_waits_then_runs_after_its_dependency(self):
        self.assertEqual(self.file(VALID_BODY), "NAT-1")
        self.assertEqual(self.file(DEPENDENT), "NAT-2")
        self.assertEqual(self.statuses(),
                         {"NAT-1": "ready", "NAT-2": "blocked_on_deps"})

        out = self.run_loop()

        self.assertEqual(self.statuses(),
                         {"NAT-1": "merged", "NAT-2": "merged"}, out)
        self.assertEqual(self.read(
            "SELECT t.linearIdentifier, r.outcome FROM runs r"
            " JOIN tickets t ON t.id = r.ticketId ORDER BY r.startedAt, r.id"),
            [("NAT-1", "merged"), ("NAT-2", "merged")])
        self.assertEqual(self.seen, [
            {"NAT-1": "in_flight", "NAT-2": "blocked_on_deps"},
            {"NAT-1": "merged", "NAT-2": "in_flight"}])
        self.assertIn("the first work", self.subjects())
        self.assertIn("the second work", self.subjects())

    def test_an_unmerged_dependency_is_stale_only_on_a_file_board(self):
        self.file(VALID_BODY)
        self.file(VALID_BODY)
        self.file(VALID_BODY)
        body = VALID_BODY.replace("Depends on: none", "Depends on: NAT-3")
        self.assertEqual(self.statuses()["NAT-3"], "ready")
        files = self.target.parent / "files"
        files.mkdir()

        native = stale_reasons(self.target, body, self.conn, self.board)
        filed = stale_reasons(self.target, body, self.conn,
                              FileProvider(files))

        self.assertEqual(native, [])
        self.assertEqual(len(filed), 1)
        self.assertIn("`NAT-3`", filed[0])

    def test_the_sweep_ends_a_finished_wait_without_listing_the_board(self):
        self.file(VALID_BODY)
        self.file(DEPENDENT)
        (first,) = self.read(
            "SELECT id FROM tickets WHERE linearIdentifier = 'NAT-1'")
        store.tickets.walk_ticket(self.conn, first[0], "merged")
        self.assertEqual(self.statuses(),
                         {"NAT-1": "merged", "NAT-2": "blocked_on_deps"})
        board = UnlistedBoard(self.project, "NAT", self.board.team)

        with closing(open_store(self.project)) as conn:
            pairs = owed(self.project, conn, self.project_id, board, 0,
                         io.StringIO(), sweep_config(self.project))

        (second,) = self.read(
            "SELECT id FROM tickets WHERE linearIdentifier = 'NAT-2'")
        self.assertEqual([ticket for ticket, _ in pairs], [second[0]])
        self.assertEqual(self.statuses()["NAT-2"], "ready")
