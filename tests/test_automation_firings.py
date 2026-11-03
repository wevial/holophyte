"""`automationFirings`: a firing's key is reserved once, in a real store.

Run: python3 -m unittest discover -s tests -p 'test_automation_firings.py' -v
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import store
import store.automations
import store.read


class FiringKeyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "store.db"

    def test_a_firing_key_is_reserved_once(self):
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        project = store.ensure_project(conn, "team-1", "/repos/p")

        def reserve(now):
            return store.automations.reserve_firing(
                conn, project, "main_watch", 1, "main", "o/p@abc run 7",
                mode="dry_run", reason="would rerun", detail={"sha": "abc"},
                now=now)

        first, second = reserve(1000), reserve(2000)

        self.assertIsInstance(first, int)
        self.assertIsNone(second)
        self.assertEqual(conn.execute(
            "SELECT id, state, createdAt FROM automationFirings").fetchall(),
            [(first, "proposed", 1000)])

    def test_a_store_an_older_build_wrote_reads_no_firings(self):
        store.open(self.path).close()
        raw = sqlite3.connect(self.path)
        raw.executescript("DROP TABLE automationFirings;\n"
                          "PRAGMA user_version = 62;\n")
        raw.close()
        conn = store.read.open_readonly(self.path)
        self.addCleanup(conn.close)

        self.assertEqual(store.automations.firings(conn), [])


if __name__ == "__main__":
    unittest.main()
