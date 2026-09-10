"""`store.heartbeat()` beats a live run and refuses an ended one (KO-339).

The beat thread's answer is what tells the loop a run was swept from under
it: True moves `lastHeartbeat`, False leaves an ended run's row untouched.
The store is read back with plain SQL over a second connection, so the
oracle is the stored column and not the function's own report.

Run: python3 -m unittest discover -s tests -p 'test_store*' -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import store


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"
        self.conn = self.open()
        store.init(self.conn)
        project = store.ensure_project(self.conn, "team_abc", "/repos/x")
        ticket = store.mirror_ticket(
            self.conn, project, "iss_1", "KO-1", "ticket one",
            acceptance_criteria=["it works"], verification_commands=["true"])
        self.run = store.claim(self.conn, project, ticket, now=1_000)

    def open(self):
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        return conn

    def last_heartbeat(self):
        return self.open().execute(
            "SELECT lastHeartbeat FROM runs WHERE id = ?",
            (self.run,)).fetchone()[0]

    def test_a_live_run_beats_and_its_heartbeat_moves(self):
        before = self.last_heartbeat()

        beat = store.heartbeat(self.conn, self.run, now=before + 5_000)

        self.assertTrue(beat)
        self.assertEqual(self.last_heartbeat(), before + 5_000)

    def test_an_ended_run_does_not_beat_and_is_left_as_it_was(self):
        store.release(self.conn, self.run, "failed",
                      "swept by the supervisor in phase working: time_box",
                      now=2_000)
        before = self.last_heartbeat()

        beat = store.heartbeat(self.conn, self.run, now=before + 5_000)

        self.assertFalse(beat)
        self.assertEqual(self.last_heartbeat(), before)


if __name__ == "__main__":
    unittest.main()
