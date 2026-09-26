"""A native board is the store, so nothing is pushed to or asked of it
(KO-759): `mirror_status()` queues no push, a stale park writes only its
`stale` note, and `observe_board()` does not ask the board its states."""
import io
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_fixture import MINUTE, T0, SweepTestCase  # noqa: E402

import store.tickets  # noqa: E402
from holophyte.board import mirror_status  # noqa: E402
from holophyte.board_sync import observe_board  # noqa: E402
from holophyte.freshness import park_stale  # noqa: E402
from holophyte.native_board import NativeBoard  # noqa: E402

ASK = 10 * MINUTE
NATIVE = '[board]\nkind = "native"\nkey = "NAT"\n'
GONE = "`holophyte/gone.py` (named in Implementation notes) is not on main"


class TrippedBoard(NativeBoard):
    """The native board, recording each board call it should never get."""

    def __init__(self, project):
        super().__init__(project, "NAT", "team-1")
        self.asked = []

    def label_issue(self, issue_id, name):
        self.asked.append(("label_issue", issue_id, name))
        raise AssertionError("a native ticket was labelled")

    def states(self, identifiers):
        self.asked.append(("states", list(identifiers)))
        raise AssertionError("a native board was asked its states")


class NativeBoardInertTests(SweepTestCase):
    def setUp(self):
        super().setUp()
        self.configure(NATIVE)
        self.board = TrippedBoard(self.project)

    def a_ready_ticket(self, identifier):
        return store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=identifier,
            linear_identifier=identifier, title=identifier,
            acceptance_criteria=["Given the ticket, then it is worked"],
            verification_commands=["echo ok"])

    def row(self, ticket):
        return self.conn.execute(
            "SELECT status, pushState, pushFrom, pushAt FROM tickets"
            " WHERE id = ?", (ticket,)).fetchone()

    def test_a_status_move_queues_no_push(self):
        ticket = self.a_ready_ticket("NAT-1")
        self.assertEqual(self.row(ticket)[0], "ready")

        self.assertTrue(mirror_status(self.conn, ticket, "in_flight",
                                      self.board))

        self.assertEqual(self.row(ticket), ("in_flight", None, None, None))

    def test_a_stale_park_writes_its_note_and_nothing_else(self):
        task = {"id": "NAT-2", "issue_id": "NAT-2", "title": "NAT-2",
                "verify": "echo ok", "budget_min": 5, "contracts": [],
                "criteria": ["Given the thing, then it works"]}
        with patch.object(sys, "stdout", io.StringIO()):
            park_stale(self.project, self.conn, self.project_id, self.board,
                       task, [GONE])

        ticket = self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = 'NAT-2'"
        ).fetchone()[0]
        self.assertEqual(self.row(ticket)[:2], ("needs_spec", None))
        notes = self.conn.execute(
            "SELECT kind, text FROM ticketNotes WHERE ticketId = ?",
            (ticket,)).fetchall()
        self.assertEqual([kind for kind, _ in notes], ["stale"])
        self.assertIn(GONE, notes[0][1])
        self.assertEqual(self.board.asked, [])

    def test_the_sweep_does_not_ask_a_native_board(self):
        ticket = self.a_ready_ticket("NAT-3")
        row = self.conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket,)).fetchone()
        revisions = self.conn.execute(
            "SELECT * FROM ticketRevisions WHERE ticketId = ?",
            (ticket,)).fetchall()
        out, asked = io.StringIO(), {self.project_id: T0 - 2 * ASK}

        observe_board(self.project, self.conn, self.project_id, self.board,
                      T0, out, asked, ASK)

        self.assertEqual(self.board.asked, [])
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(self.conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket,)).fetchone(), row)
        self.assertEqual(self.conn.execute(
            "SELECT * FROM ticketRevisions WHERE ticketId = ?",
            (ticket,)).fetchall(), revisions)
