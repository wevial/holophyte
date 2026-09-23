"""KO-665: schema repair and project-level interventions through the store API.

The broken store is made the way the `interventions_old` incident made it: a
table rebuild that renames the live `interventions` away, so SQLite follows
the rename in `runs.stopRequested`, and then drops the renamed table.

Run: HOLOPHYTE_HOME=$(mktemp -d) python3 -m unittest tests.test_store_repair
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import store
import store.schema
import store.tickets

DANGLING = ("runs", "stopRequested", "interventions_old", "interventions")


class RepairReferencesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"
        conn = store.open(self.path, migrate="owner")
        self.project_id = store.tickets.ensure_project(conn, "team", "/repo")
        conn.close()

    def raw(self):
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        return conn

    def break_interventions(self, conn):
        (ddl,) = conn.execute("SELECT sql FROM sqlite_master"
                              " WHERE name = 'interventions'").fetchone()
        conn.execute("ALTER TABLE interventions RENAME TO interventions_old")
        conn.execute(ddl)
        conn.execute("INSERT INTO interventions SELECT * FROM interventions_old")
        conn.execute("DROP TABLE interventions_old")
        conn.commit()

    @staticmethod
    def schema(conn):
        return conn.execute("SELECT name, sql FROM sqlite_master"
                            " ORDER BY name").fetchall()

    @staticmethod
    def interventions(conn):
        return conn.execute('SELECT runId, projectId, "action", note'
                            " FROM interventions").fetchall()

    def test_dry_run_reports_the_dangling_key_and_changes_nothing(self):
        conn = self.raw()
        self.break_interventions(conn)
        with self.assertRaises(store.schema.SchemaError):
            store.open(self.path)
        schema, rows = self.schema(conn), self.interventions(conn)

        self.assertEqual(store.repair_references(conn), [DANGLING])

        self.assertEqual(self.schema(conn), schema)
        self.assertEqual(self.interventions(conn), rows)

    def test_repair_rewrites_the_key_and_records_the_decision_first(self):
        conn = self.raw()
        self.break_interventions(conn)
        rows = self.interventions(conn)

        self.assertEqual(store.repair_references(conn, dry_run=False),
                         [DANGLING])

        self.assertEqual([row[2] for row in conn.execute(
            "PRAGMA foreign_key_list(runs)") if row[3] == "stopRequested"],
            ["interventions"])
        ((run, project, action, note),) = self.interventions(conn)[len(rows):]
        self.assertEqual((run, project, action), (None, self.project_id, "migrate"))
        self.assertTrue(Path(json.loads(note)["backup"]).is_file())
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchall(),
                         [("ok",)])
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        # The store opens again, foreign keys on, and a run can be inserted.
        reopened = store.open(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.execute("PRAGMA foreign_keys").fetchone(), (1,))
        ticket = store.tickets.mirror_ticket(
            reopened, self.project_id, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="ticket 1")
        run_id = store.claim(reopened, self.project_id, ticket,
                             now=1_700_000_000_000)
        self.assertEqual(reopened.execute(
            "SELECT COUNT(*) FROM runs WHERE id = ?", (run_id,)).fetchone(), (1,))

    def test_a_repair_that_leaves_any_orphan_is_rolled_back(self):
        conn = self.raw()
        self.break_interventions(conn)
        # An orphan in a table the repair does not touch: there is no run 999.
        conn.execute("INSERT INTO runEvents (runId, seq, level, kind, summary,"
                     " at) VALUES (999, 1, 'narrative', 'note', 'orphan', 0)")
        conn.commit()
        schema, rows = self.schema(conn), self.interventions(conn)

        with self.assertRaisesRegex(sqlite3.DatabaseError, "rolled back"):
            store.repair_references(conn, dry_run=False)

        self.assertEqual(self.schema(conn), schema)
        self.assertEqual(self.interventions(conn), rows)
        self.assertEqual(conn.execute("PRAGMA writable_schema").fetchone(), (0,))

    def test_a_repair_inside_an_open_transaction_is_refused_promptly(self):
        # The backup cannot progress past the caller's own write transaction,
        # so this call once hung holding the writer lock: run it in a child
        # with a deadline, and the suite fails instead of hanging with it.
        self.break_interventions(self.raw())
        child = (
            "import sqlite3, sys, store\n"
            "conn = sqlite3.connect(sys.argv[1])\n"
            "with store.transaction(conn):\n"
            "    store.record_project_intervention(conn, 'migrate', 'first')\n"
            "    try:\n"
            "        store.repair_references(conn, dry_run=False)\n"
            "    except ValueError as exc:\n"
            "        print('refused:', exc)\n")
        root = Path(__file__).resolve().parents[1]

        result = subprocess.run(
            [sys.executable, "-c", child, str(self.path)], cwd=root,
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(root)})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("refused: repair_references", result.stdout)
        self.assertEqual(store.repair_references(self.raw()), [DANGLING])

    def test_only_the_reference_clause_is_rewritten(self):
        conn = self.raw()
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE child (id INTEGER PRIMARY KEY,"
                     " parentId INTEGER REFERENCES parent_old (id),"
                     " label TEXT DEFAULT 'REFERENCES parent_old(id)')")
        conn.commit()

        self.assertEqual(store.repair_references(conn, dry_run=False),
                         [("child", "parentId", "parent_old", "parent")])

        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("INSERT INTO parent (id) VALUES (1)")
        conn.execute("INSERT INTO child (id, parentId) VALUES (1, 1)")
        self.assertEqual(conn.execute("SELECT label FROM child").fetchone(),
                         ("REFERENCES parent_old(id)",))

    def test_a_key_with_no_base_table_is_reported_and_never_rewritten(self):
        conn = self.raw()
        conn.execute("CREATE TABLE strays (id INTEGER PRIMARY KEY,"
                     " ghostId INTEGER REFERENCES ghosts_old (id))")
        conn.commit()
        schema, rows = self.schema(conn), self.interventions(conn)
        unresolved = [("strays", "ghostId", "ghosts_old", None)]

        self.assertEqual(store.repair_references(conn), unresolved)
        self.assertEqual(store.repair_references(conn, dry_run=False),
                         unresolved)

        self.assertEqual(self.schema(conn), schema)
        self.assertEqual(self.interventions(conn), rows)


class RecordProjectInterventionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(Path(tmp.name) / "store.sqlite3", migrate="owner")
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(self.conn, "team", "/repo")

    def test_records_one_row_with_no_run_and_the_project(self):
        before = self.conn.execute(
            "SELECT COUNT(*) FROM interventions").fetchone()[0]

        store.record_project_intervention(self.conn, "migrate", "note")

        self.assertEqual(self.conn.execute(
            'SELECT runId, projectId, "action", note FROM interventions'
            " ORDER BY id DESC LIMIT 1").fetchone(),
            (None, self.project_id, "migrate", "note"))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM interventions").fetchone()[0], before + 1)

    def test_an_unknown_action_is_refused_before_sqlite_is_asked(self):
        # A closed connection fails any statement with ProgrammingError, so
        # only validation that runs first can answer with ValueError.
        self.conn.close()
        with self.assertRaisesRegex(ValueError, "unknown intervention action"):
            store.record_project_intervention(self.conn, "reboot", "note")


if __name__ == "__main__":
    unittest.main()
