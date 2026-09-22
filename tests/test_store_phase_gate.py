"""The phase writer refuses illegal edges without changing durable evidence."""
import sqlite3
import unittest
from contextlib import closing

import store
from tests import loop_fixture
from tests import test_store_resume as resume_tests


def audit_phase_events(test, conn):
    """Audit persisted loop evidence before its fixture store is removed."""
    rows = conn.execute(
        "SELECT runId, summary FROM runEvents WHERE kind = 'phase_change'").fetchall()
    for run_id, summary in rows:
        previous, phase = summary.split(':', 1)[0].split(' -> ')
        test.assertTrue(
            previous == phase or phase in store.RUN_PHASE_TRANSITIONS[previous],
            f"run {run_id}: {summary}")
    return len(rows)


class PhaseGateTests(unittest.TestCase):
    setUp = resume_tests.ResumeTests.setUp
    a_run = resume_tests.ResumeTests.a_run

    def test_refusal_preserves_row_heartbeat_and_events(self):
        run = self.a_run('merge_gate')
        before = self.conn.execute('SELECT * FROM runs WHERE id = ?', (run,)).fetchone()
        events = self.conn.execute('SELECT * FROM runEvents').fetchall()
        with self.assertRaises(store.IllegalTransition) as caught:
            store.set_phase(self.conn, run, 'working', now=9000)
        error = caught.exception
        self.assertEqual((error.run_id, error.previous, error.phase),
                         (run, 'merge_gate', 'working'))
        self.assertIn(f'run {run}', str(error))
        self.assertIn('merge_gate -> working', str(error))
        self.assertEqual(self.conn.execute('SELECT * FROM runs WHERE id = ?',
                                          (run,)).fetchone(), before)
        self.assertEqual(
            self.conn.execute('SELECT * FROM runEvents').fetchall(), events)

    def test_done_has_no_exit_even_before_ended_at_is_stamped(self):
        run = self.a_run('done')
        with self.assertRaises(store.IllegalTransition):
            store.set_phase(self.conn, run, 'working', now=9000)
        self.assertEqual(self.conn.execute(
            'SELECT phase, lastHeartbeat FROM runs WHERE id = ?', (run,)).fetchone(),
            ('done', 2000))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM runEvents').fetchone(),
                         (0,))

    def test_note_update_and_forward_transition(self):
        run = self.a_run('working')
        store.set_phase(self.conn, run, 'working', note='new note', now=3000)
        store.set_phase(self.conn, run, 'verifying', now=4000)
        self.assertEqual(self.conn.execute(
            'SELECT phase, lastHeartbeat FROM runs WHERE id = ?', (run,)).fetchone(),
            ('verifying', 4000))
        self.assertEqual(self.conn.execute(
            'SELECT summary FROM runEvents WHERE runId = ? ORDER BY seq',
            (run,)).fetchall(),
            [('working -> working: new note',), ('working -> verifying',)])
        self.assertEqual(audit_phase_events(self, self.conn), 2)


def audit_loop_store(test):
    """Shared loop fixtures call this while their temporary store still exists."""
    if test.db.exists():
        with closing(sqlite3.connect(test.db)) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'runEvents'").fetchone()
            if table:
                audit_phase_events(test, conn)


class LoopPhaseAuditTests(loop_fixture.LoopFixture):
    def test_persisted_loop_stream_obeys_the_graph(self):
        from tests.fake_agent import APPROVE, Commit

        self.loop(Commit("candidate"), APPROVE)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertGreater(audit_phase_events(self, conn), 0)
            self.assertEqual(conn.execute("SELECT outcome FROM runs").fetchall(),
                             [("merged",)])
