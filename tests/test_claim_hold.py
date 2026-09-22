"""Project admission stops before the board, and is checked on every claim."""

import contextlib
import io
import sqlite3
from unittest.mock import Mock

import store
from holophyte.claim import _claim_next
from holophyte.dispatch import _mirror_queue, _startup_sweep
from holophyte.reconcile import _reconcile_at_startup
from tests.sweep_fixture import SweepTestCase


class ClaimHoldTests(SweepTestCase):
    def test_hold_skips_ready_board_and_store_claim_until_released(self):
        run = self.a_run()
        ticket = self.ticket_of[run]
        store.release(self.conn, run, "failed", "retry")
        provider = Mock()
        provider.claim_next.return_value = {"id": "KO-2"}
        store.hold(self.conn, self.project, "reboot pending")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _startup_sweep(self.tgt, self.conn)
            self.assertIn("held: reboot pending", out.getvalue())
            _reconcile_at_startup(self.tgt, self.conn, self.project, provider)
            _mirror_queue(self.tgt, self.conn, self.project, provider)
            result = _claim_next(
                self.tgt, self.conn, self.project, provider, "identifier", set(), None
            )
        self.assertEqual(result, (None, None, None))
        self.assertEqual(provider.mock_calls, [])
        self.assertIn(str(self.target), out.getvalue())
        self.assertIn("held: reboot pending", out.getvalue())
        self.assertIsNone(store.claim(self.conn, self.project, ticket))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM runs").fetchone(), (1,)
        )
        store.release_hold(self.conn, self.project, "reboot complete")
        self.assertIsNotNone(store.claim(self.conn, self.project, ticket))

    def test_failed_project_update_rolls_back_intervention(self):
        self.conn.executescript("""
            CREATE TRIGGER refuse_hold BEFORE UPDATE OF admission ON projects
            BEGIN SELECT RAISE(ABORT, 'write refused'); END;
        """)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'write refused'):
            store.hold(self.conn, self.project, 'reboot pending')
        self.assertEqual(self.conn.execute(
            "SELECT admission FROM projects WHERE id = ?",
            (self.project,)).fetchone(), ('enabled',))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM interventions WHERE action = 'hold'"
        ).fetchone(), (0,))
