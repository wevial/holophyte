"""`automationFirings`: a firing's key is reserved once, in a real store.

Run: python3 -m unittest discover -s tests -p 'test_automation_firings.py' -v
"""
import tempfile
import unittest
from pathlib import Path

import store
import store.automations


class FiringKeyTests(unittest.TestCase):
    def test_a_firing_key_is_reserved_once(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        conn = store.open(Path(directory.name) / "store.db")
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


if __name__ == "__main__":
    unittest.main()
