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


if __name__ == "__main__":
    unittest.main()
