"""In store mode a ledger entry and its board note are one transaction and
nothing is posted inline; the escalation's comment is a note too (KO-742).
`ledger()`, `escalate()` and `store.record_note()` on a real store."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loop_fixture import StubProvider  # noqa: E402
from sweep_fixture import SweepTestCase  # noqa: E402

import store  # noqa: E402
from holophyte import redact  # noqa: E402
from holophyte.board import BOARD_COMMENT_LIMIT, escalate, ledger  # noqa: E402

SENTINEL = "ticket-notes-sentinel-742"


class StoreModeBoard(StubProvider):
    store_mode = True


class TicketNoteTests(SweepTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(redact, "_environment_values",
                                       frozenset({SENTINEL})))

    def rows(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def notes(self, ticket):
        return self.rows("SELECT kind, text, runId, dedupKey FROM ticketNotes"
                         " WHERE ticketId = ?", ticket)

    def test_store_mode_ledger_writes_a_note_and_mirror_mode_posts(self):
        tail = "the last line of the round"
        text = (f"Round 1 saw {SENTINEL}.\n" + "x" * BOARD_COMMENT_LIMIT
                + f"\n{tail}")
        run = self.a_run()
        board = StoreModeBoard()
        ledger(self.conn, run, "KO-1", "round", text, board)
        self.assertEqual(board.comments, [])
        (entry,) = self.rows("SELECT text FROM ledger WHERE runId = ?", run)
        self.assertTrue(entry[0].endswith(tail), "the entry is kept whole")
        (note,) = self.notes(self.ticket_of[run])
        kind, body, note_run, _ = note
        self.assertEqual((kind, note_run), ("ledger", run))
        self.assertNotIn(SENTINEL, body)
        self.assertIn("[redacted]", body)
        self.assertNotIn(tail, body)
        self.assertIn("characters cut", body)

        mirror_run = self.a_run()
        mirror = StubProvider()
        ledger(self.conn, mirror_run, "KO-2", "round", text, mirror)
        self.assertEqual(len(mirror.comments), 1)
        self.assertEqual(self.notes(self.ticket_of[mirror_run]), [])

    def test_a_failed_note_leaves_no_entry_and_a_key_is_one_note(self):
        run = self.a_run()
        with patch.object(store, "record_note",
                          side_effect=RuntimeError("note refused")):
            with self.assertRaises(RuntimeError):
                ledger(self.conn, run, "KO-1", "round", "a round",
                       StoreModeBoard())
        self.assertEqual(self.rows("SELECT id FROM ledger"), [])
        self.assertEqual(self.rows("SELECT id FROM ticketNotes"), [])

        ticket = self.ticket_of[run]
        self.assertIsNotNone(store.record_note(self.conn, ticket, "ledger",
                                               "first", "ledger:5"))
        self.assertIsNone(store.record_note(self.conn, ticket, "ledger",
                                            "again", "ledger:5"))
        self.assertEqual(self.notes(ticket),
                         [("ledger", "first", None, "ledger:5")])

    def test_store_mode_escalation_parks_and_writes_its_history_as_a_note(self):
        first = self.a_run()
        ticket = self.ticket_of[first]
        store.release(self.conn, first, "failed", "verify failed: first")
        second = self.a_run(ticket=ticket)
        store.release(self.conn, second, "failed", "verify failed: second")
        board = StoreModeBoard()
        self.assertTrue(escalate(self.conn, ticket, board))
        (status,) = self.rows("SELECT status FROM tickets WHERE id = ?",
                              ticket)
        self.assertEqual(status[0], "blocked_on_operator")
        self.assertEqual(board.comments, [])
        (note,) = self.notes(ticket)
        kind, body, note_run, key = note
        self.assertEqual((kind, note_run, key),
                         ("escalation", second, f"escalation:{second}"))
        self.assertIn("verify failed: first", body)
        self.assertIn("verify failed: second", body)
