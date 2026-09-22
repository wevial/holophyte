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
from holophyte.schema_owner import migrate_store
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

    def test_read_modes_and_loop_refuse_until_supervisor_migrates(self):
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
                    f"{store.SCHEMA_VERSION}; start the supervisor",
                    result.stderr)
                self.assertEqual(self.version(fixture), store.SCHEMA_VERSION - 1)
        with self.assertRaisesRegex(store.SchemaOlder, "supervisor"):
            open_store(fixture.target)
        migrate_store(fixture.target)
        open_store(fixture.target).close()
        self.assertEqual(self.version(fixture), store.SCHEMA_VERSION)

    def test_acting_sweep_refuses_to_migrate(self):
        fixture = self.fixture(test_cli_approve.ApproveCliTests)
        self.stamp_older(fixture)
        with patch("holophyte.sweep_report.review_container_lines", return_value=[]):
            with self.assertRaisesRegex(store.SchemaOlder, "supervisor"):
                fixture.cli("--sweep", "--act")
        self.assertEqual(self.version(fixture), store.SCHEMA_VERSION - 1)

    def test_write_commands_refuse_until_owner_migrates(self):
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
                with self.assertRaisesRegex(store.SchemaOlder, "supervisor"):
                    fixture.cli(mode, "KO-1", "--note", "continue")
                self.assertEqual(self.version(fixture), store.SCHEMA_VERSION - 1)
                self.assertEqual(fixture.interventions(), [])
                migrate_store(fixture.target)
                fixture.cli(mode, "KO-1", "--note", "continue")
                self.assertEqual(self.version(fixture), store.SCHEMA_VERSION)
                action = "operator_note" if mode == "--babysit" else mode[2:]
                self.assertEqual(fixture.interventions(), [(fixture.run, action)])
                self.assertEqual(fixture.conn.execute(
                    "SELECT status FROM tickets WHERE id = ?",
                    (fixture.ticket,)).fetchone(), ("ready",))
