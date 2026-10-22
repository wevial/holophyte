"""`GET /status` serves under `queued` only a ready ticket the loop could
claim: one in the Backlog column is left out. A real store.

Run: python3 -m unittest discover -s tests -p 'test_serve_queued_backlog.py' -v
"""
from __future__ import annotations

import contextlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve_fixture import MIN, ServeTestCase  # noqa: E402 - after the insert

import store  # noqa: E402 - after the sys.path insert above
import store.board  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from tests.phase_fixture import finish_run  # noqa: E402 - after the insert
from tests.test_store_board import body  # noqa: E402 - after the insert


class QueuedBacklogTests(ServeTestCase):
    def queued(self):
        _, _, served = self.request("GET", "/status")
        return [q["ticket"] for q in served["queued"]]

    def test_a_requeued_ticket_moved_to_backlog_is_not_queued(self):
        self.seed()
        with contextlib.closing(store.open(str(self.db))) as conn:
            project_id = store.tickets.ensure_project(
                conn, "team-1", self.target)
            store.board.file_ticket(conn, project_id, "NAT", body())
            ticket = conn.execute("SELECT id FROM tickets WHERE"
                                  " linearIdentifier = 'NAT-1'").fetchone()[0]
            run = store.claim(conn, project_id, ticket, now=self.now - 10 * MIN)
            store.tickets.transition(conn, ticket, "in_flight")
            store.set_phase(conn, run, "working", now=self.now - 10 * MIN)
            finish_run(conn, run, "failed", now=self.now - 5 * MIN)
            store.requeue(conn, ticket, "retry", now=self.now - 4 * MIN)
        self.start()
        self.assertIn("NAT-1", self.queued())

        with contextlib.closing(store.open(str(self.db))) as conn:
            (revision,) = conn.execute(
                "SELECT revision FROM tickets WHERE id = ?",
                (ticket,)).fetchone()
            store.board.move_ticket(conn, project_id, "NAT-1", "backlog",
                                    revision)
        self.assertNotIn("NAT-1", self.queued())


if __name__ == "__main__":
    unittest.main()
