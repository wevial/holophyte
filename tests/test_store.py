"""Store connections wait for a lock instead of crashing the run (KO-286).

WAL admits one writer at a time, and the loop, its heartbeat thread and the
supervisor's sweep are three writers on one file. Run 103 died at a phase
change with `database is locked` because the sqlite3 default wait of five
seconds was shorter than a sweep under load. `store.open()` waits
`store.BUSY_TIMEOUT_S` instead; the tests hold a write lock on one
connection and show a second one waits it out, and that the wait is still a
bound rather than a hang.

Run: python3 -m unittest discover -s tests -p 'test_store.py' -v
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import store


def hold_write_lock(path, seconds, held):
    """Take the store's write lock, signal `held`, keep it for `seconds`.

    Opens its own connection: sqlite3 refuses one shared across threads.
    """
    conn = store.open(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        held.set()
        time.sleep(seconds)
        conn.execute("COMMIT")
    finally:
        conn.close()


class BusyTimeoutTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "holophyte.db")
        self.reader = store.open(self.path)
        self.addCleanup(self.reader.close)
        self.reader.execute("CREATE TABLE probe (n INTEGER)")
        self.reader.commit()

    def start_hold(self, seconds):
        held = threading.Event()
        thread = threading.Thread(
            target=hold_write_lock, args=(self.path, seconds, held))
        thread.start()
        self.addCleanup(thread.join)
        self.assertTrue(held.wait(5), "the holder never took the lock")
        return held

    def test_writer_waits_for_a_held_lock(self):
        # The hold is longer than nothing but shorter than the wait, so the
        # second writer's only path to success is to block until COMMIT.
        self.start_hold(2)
        writer = store.open(self.path)
        self.addCleanup(writer.close)
        started = time.monotonic()
        writer.execute("INSERT INTO probe VALUES (1)")
        writer.commit()
        waited = time.monotonic() - started
        self.assertGreater(waited, 1.0, "the write did not wait for the lock")
        self.assertEqual(
            self.reader.execute("SELECT count(*) FROM probe").fetchone(),
            (1,))

    def test_wait_is_bounded(self):
        self.start_hold(2)
        with patch.object(store, "BUSY_TIMEOUT_S", 0.2):
            writer = store.open(self.path)
        self.addCleanup(writer.close)
        with self.assertRaises(sqlite3.OperationalError) as caught:
            writer.execute("INSERT INTO probe VALUES (1)")
            writer.commit()
        self.assertIn("locked", str(caught.exception))
        # Both the connect() argument and the pragma carried the override.
        self.assertEqual(
            writer.execute("PRAGMA busy_timeout").fetchone(), (200,))

    def test_open_sets_the_configured_wait(self):
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("PRAGMA busy_timeout").fetchone(),
            (store.BUSY_TIMEOUT_S * 1000,))


MINUTE = 60 * 1000
T0 = 1_700_000_000_000
OLD_SHA = "f662448400000000000000000000000000000000"
NEW_SHA = "55f6d7f000000000000000000000000000000000"


class RepointTests(unittest.TestCase):
    """`store.repoint()` (KO-297): a parked candidate moved to a rebuilt
    branch tip as a recorded intervention, refusing everything else."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(str(Path(tmp.name) / "store.sqlite3"))
        self.addCleanup(self.conn.close)
        self.project = store.ensure_project(self.conn, "team-1", tmp.name)
        self.ticket = store.mirror_ticket(
            self.conn, self.project, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="a ticket",
            acceptance_criteria=["Given a ticket, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MINUTE)
        store.transition(self.conn, self.ticket, "in_flight")
        self.run = store.claim(self.conn, self.project, self.ticket, now=T0)
        for phase in ("working", "verifying", "reviewing", "merge_gate"):
            store.set_phase(self.conn, self.run, phase, now=T0 + MINUTE)

    def park(self):
        store.transition(self.conn, self.ticket, "blocked_on_operator")
        store.park(self.conn, self.run, "awaiting_merge_approval",
                   candidate_sha=OLD_SHA, now=T0 + 2 * MINUTE)

    def candidate_sha(self):
        return self.conn.execute(
            "SELECT candidateSha FROM runs WHERE id = ?",
            (self.run,)).fetchone()[0]

    def rows(self, sql):
        return self.conn.execute(sql, (self.run,)).fetchall()

    def test_moves_the_sha_and_records_both_rows(self):
        self.park()

        result = store.repoint(self.conn, self.ticket, NEW_SHA,
                               "rebuilt on the filtered main",
                               now=T0 + 3 * MINUTE)

        self.assertEqual(result, (self.run, OLD_SHA))
        self.assertEqual(self.candidate_sha(), NEW_SHA)
        self.assertEqual(
            self.rows('SELECT source, "trigger", "action", guidance, at'
                      " FROM interventions WHERE runId = ?"),
            [("human", "manual", "repoint", "rebuilt on the filtered main",
              T0 + 3 * MINUTE)])
        summaries = [summary for (summary,) in self.rows(
            "SELECT summary FROM runEvents WHERE runId = ?"
            " AND level = 'narrative' ORDER BY seq")]
        naming_both = [text for text in summaries
                       if OLD_SHA in text and NEW_SHA in text]
        self.assertEqual(len(naming_both), 1, summaries)
        self.assertIn("rebuilt on the filtered main", naming_both[0])
        # Still parked: the re-point moves the sha and nothing else, so the
        # approval that follows is the operator's separate decision.
        self.assertEqual(
            self.conn.execute("SELECT phase, endedAt FROM runs WHERE id = ?",
                              (self.run,)).fetchone(),
            ("awaiting_merge_approval", None))

    def test_refuses_unparked_and_malformed(self):
        # Live, at the merge gate but not parked: the sha is not a park's.
        with self.assertRaises(store.RepointRefused) as live:
            store.repoint(self.conn, self.ticket, NEW_SHA, "rebuilt")
        self.assertIn("KO-1", str(live.exception))
        self.assertIn(f"run {self.run}", str(live.exception))
        # Ended without parking: nothing waits at the gate.
        store.release(self.conn, self.run, "failed", "verify",
                      now=T0 + 2 * MINUTE)
        with self.assertRaises(store.RepointRefused) as failed:
            store.repoint(self.conn, self.ticket, NEW_SHA, "rebuilt")
        self.assertIn("KO-1", str(failed.exception))
        self.assertIn("not awaiting_merge_approval", str(failed.exception))
        with self.assertRaises(store.RepointRefused) as unknown:
            store.repoint(self.conn, self.ticket + 1, NEW_SHA, "rebuilt")
        self.assertIn(str(self.ticket + 1), str(unknown.exception))
        self.assertEqual(self.candidate_sha(), None)

        # Parked, but the sha is not a full commit id: abbreviated, upper
        # case, a branch name, not text.
        self.conn.execute("UPDATE runs SET endedAt = NULL, outcome = NULL"
                          " WHERE id = ?", (self.run,))
        self.conn.execute("UPDATE runs SET phase = 'merge_gate' WHERE id = ?",
                          (self.run,))
        self.conn.execute(
            "UPDATE tickets SET activeRunId = ?, lastRunId = NULL"
            " WHERE id = ?", (self.run, self.ticket))
        self.conn.commit()
        self.park()
        for bad in (NEW_SHA[:7], NEW_SHA.upper(), "main", NEW_SHA + "0", None):
            with self.subTest(sha=bad):
                with self.assertRaises(store.RepointRefused) as malformed:
                    store.repoint(self.conn, self.ticket, bad, "rebuilt")
                self.assertIn(str(self.ticket), str(malformed.exception))
                self.assertIn("40-hex", str(malformed.exception))
        with self.assertRaises(ValueError):
            store.repoint(self.conn, self.ticket, NEW_SHA, "   ")

        self.assertEqual(self.candidate_sha(), OLD_SHA)
        self.assertEqual(self.rows("SELECT 1 FROM interventions"
                                   " WHERE runId = ?"), [])
        self.assertEqual(self.rows("SELECT 1 FROM runEvents WHERE runId = ?"
                                   " AND kind = 'repoint'"), [])


if __name__ == "__main__":
    unittest.main()
