"""Schema ownership at the factory command boundary (KO-494)."""
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import store
from holophyte.runs import open_store
from tests import test_cli_approve, test_cli_requeue


class FactorySchemaCliTests(unittest.TestCase):
    def fixture(self, cls):
        fixture = cls()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def stamp_older(self, fixture):
        fixture.conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION - 1}")

    def version(self, fixture):
        with sqlite3.connect(fixture.target.store_path) as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    def test_read_modes_refuse_older_then_loop_open_migrates(self):
        fixture = self.fixture(test_cli_approve.ApproveCliTests)
        self.stamp_older(fixture)
        factory = Path(__file__).resolve().parents[1] / "factory.py"
        for mode in ("--report", "--sweep"):
            with self.subTest(mode=mode):
                result = subprocess.run(
                    [sys.executable, str(factory), str(fixture.repo), mode],
                    capture_output=True, text=True, env=os.environ.copy())
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    f"store at {fixture.target.store_path} is schema "
                    f"{store.SCHEMA_VERSION - 1}; this build expects "
                    f"{store.SCHEMA_VERSION}; start the loop or the serve daemon",
                    result.stderr)
                self.assertEqual(self.version(fixture), store.SCHEMA_VERSION - 1)
        open_store(fixture.target).close()
        self.assertEqual(self.version(fixture), store.SCHEMA_VERSION)

    def test_acting_sweep_migrates(self):
        fixture = self.fixture(test_cli_approve.ApproveCliTests)
        self.stamp_older(fixture)
        with patch("holophyte.sweep_report.review_container_lines", return_value=[]):
            out, _ = fixture.cli("--sweep", "--act")
        self.assertIn("no runs in flight", out)
        self.assertEqual(self.version(fixture), store.SCHEMA_VERSION)

    def test_write_commands_migrate_and_release_the_ticket(self):
        for mode in ("--requeue", "--babysit", "--approve"):
            with self.subTest(mode=mode):
                cls = (test_cli_requeue.RequeueCliTests if mode == "--requeue"
                       else test_cli_approve.ApproveCliTests)
                fixture = self.fixture(cls)
                if mode == "--requeue":
                    fixture.fail_the_run()
                else:
                    fixture.park(pr_url="https://example.test/pull/7")
                self.stamp_older(fixture)
                fixture.cli(mode, "KO-1", "--note", "continue")
                self.assertEqual(self.version(fixture), store.SCHEMA_VERSION)
                self.assertEqual(fixture.interventions(),
                                 [(fixture.run, mode[2:])])
                self.assertEqual(fixture.conn.execute(
                    "SELECT status FROM tickets WHERE id = ?",
                    (fixture.ticket,)).fetchone(), ("ready",))
