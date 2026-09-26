"""KO-753: `store.board` moves and cancels a native ticket at the revision
it was read at, a cancel reaching a live run as a Linear cancel does, and
`resolve_dependencies()` walks a dependency wait back to `ready`. Real
SQLite files, a real git repository as the project's.

Run: python3 -m unittest discover -s tests -p 'test_store_board_moves.py' -v
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402 - after the sys.path insert above
import store.board  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from tests.test_store_board import body  # noqa: E402 - after the sys.path insert


class StoreBoardMoveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.conn = store.open(Path(tmp.name) / "store.sqlite3")
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, "team-1", str(repo))

    def file(self, text=None, column="ready"):
        return store.board.file_ticket(self.conn, self.project_id, "NAT",
                                       text or body(), column=column)

    def ticket_id(self, identifier):
        return self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?",
            (identifier,)).fetchone()[0]

    def row(self, identifier):
        return self.conn.execute(
            "SELECT status, boardColumn, boardState, revision FROM tickets"
            " WHERE linearIdentifier = ?", (identifier,)).fetchone()

    def notes(self, identifier):
        return self.conn.execute(
            "SELECT n.kind, n.text FROM ticketNotes n JOIN tickets t"
            " ON t.id = n.ticketId WHERE t.linearIdentifier = ?"
            " ORDER BY n.id", (identifier,)).fetchall()

    def run_on(self, identifier):
        ticket_id = self.ticket_id(identifier)
        run_id = store.claim(self.conn, self.project_id, ticket_id)
        store.tickets.transition(self.conn, ticket_id, "in_flight")
        store.set_phase(self.conn, run_id, "working")
        return run_id

    def test_a_move_records_its_column_and_refuses_a_draft_or_stale_read(self):
        self.file()
        self.file(body(what=False), column="backlog")

        revision = store.board.move_ticket(
            self.conn, self.project_id, "NAT-1", "backlog", 1, note="later")
        self.assertEqual(revision, 2)
        self.assertEqual(self.row("NAT-1"), ("ready", "backlog", "Backlog", 2))
        self.assertEqual(self.notes("NAT-1"), [("move", "later")])

        with self.assertRaises(store.board.FilingRefused) as refused:
            store.board.move_ticket(self.conn, self.project_id, "NAT-2",
                                    "ready", 1)
        self.assertIn("What", str(refused.exception))
        self.assertEqual(self.row("NAT-2")[1:], ("backlog", None, 1))

        with self.assertRaises(store.RevisionMoved) as moved:
            store.board.move_ticket(self.conn, self.project_id, "NAT-1",
                                    "ready", 1, note="now")
        self.assertEqual(moved.exception.current, 2)
        self.assertEqual(self.row("NAT-1"), ("ready", "backlog", "Backlog", 2))
        self.assertEqual(self.notes("NAT-1"), [("move", "later")])

    def test_a_cancel_abandons_idle_aborts_live_and_leaves_parked(self):
        for _ in range(5):
            self.file()
        live = self.run_on("NAT-4")
        parked = self.run_on("NAT-5")
        store.set_phase(self.conn, parked, "verifying")
        store.park(self.conn, parked, "awaiting_merge_approval")
        store.tickets.transition(self.conn, self.ticket_id("NAT-5"),
                                 "blocked_on_operator")

        for identifier in ("NAT-3", "NAT-4", "NAT-5"):
            revision = store.board.cancel_ticket(
                self.conn, self.project_id, identifier, 1, "wrong scope")
            self.assertEqual(revision, 2)
            self.assertEqual(self.row(identifier)[1:],
                             ("canceled", "Canceled", 2))
            self.assertEqual(self.notes(identifier),
                             [("cancel", "wrong scope")])

        self.assertEqual(self.row("NAT-3")[0], "abandoned")
        self.assertEqual(self.row("NAT-4")[0], "in_flight")
        stop = self.conn.execute(
            'SELECT i."action", i.source, i."trigger", i.guidance, r.endedAt'
            " FROM runs r JOIN interventions i ON i.id = r.stopRequested"
            " WHERE r.id = ?", (live,)).fetchone()
        self.assertEqual(stop, ("abort", "human", "manual", "wrong scope",
                                None))
        self.assertEqual(self.row("NAT-5")[0], "blocked_on_operator")
        self.assertEqual(self.conn.execute(
            "SELECT phase, endedAt, stopRequested FROM runs WHERE id = ?",
            (parked,)).fetchone(), ("awaiting_merge_approval", None, None))

        with self.assertRaises(store.board.FilingRefused):
            store.board.cancel_ticket(self.conn, self.project_id, "NAT-3", 2,
                                      "again")

    def test_resolve_dependencies_readies_a_wait_whose_dependencies_merged(self):
        for _ in range(5):
            self.file()
        self.file(body(depends="NAT-1"))
        self.file()
        self.file()
        store.board.edit_ticket(self.conn, self.project_id, "NAT-7",
                                body(depends="NAT-8"), 1)
        self.assertEqual(self.row("NAT-6")[0], "blocked_on_deps")
        self.assertEqual(self.row("NAT-7")[0], "blocked_on_deps")

        self.assertEqual(store.board.resolve_dependencies(
            self.conn, self.project_id), [])
        store.tickets.walk_ticket(self.conn, self.ticket_id("NAT-1"), "merged")
        self.assertEqual(store.board.resolve_dependencies(
            self.conn, self.project_id), ["NAT-6"])
        self.assertEqual(self.row("NAT-6")[0], "ready")
        self.assertEqual(self.row("NAT-7")[0], "blocked_on_deps")


if __name__ == "__main__":
    unittest.main()
