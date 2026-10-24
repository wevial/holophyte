"""The `guardrails` scout kind: a store at the version before it carries its
scouts over and takes the kind, and `holo scout` launches it.

Run: python3 -m unittest discover -s tests -p 'test_scout_guardrails_kind.py' -v
"""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import store
import store.schema
from holophyte.holo.cli import main as holo_main
from store.scouts import (
    create_scout,
    end_scout,
    move_scout,
    record_scout_actions,
    record_scout_event,
    record_scout_report,
    record_scout_tasks,
    scout,
)
from store.tickets import ensure_project
from tests.test_scout_launch import LaunchCase, scout_rows
from tests.test_serve_scouts import ACTIONS, REPORT

ASK = "Mine the findings"
OLD_KINDS = "CHECK (kind IN ('bug', 'research', 'recon'))"


def old_store(path):
    """A store stamped one version below this build's, its `scouts` table
    rebuilt with the three-kind CHECK, holding a done recon scout with a
    report, a task, an action item and two events; the scout's id."""
    conn = store.open(path)
    project = ensure_project(conn, "native:ALPHA", "/repo")
    scout_id = create_scout(conn, project, "Map the merge gate", "recon", 20)
    move_scout(conn, scout_id, "planning", base_sha="a" * 40)
    record_scout_event(conn, scout_id, "scout_planned", "the lead planned 1")
    move_scout(conn, scout_id, "swarming")
    record_scout_tasks(conn, scout_id, 1, 1, [
        {"question": "Where is the gate?", "paths": [], "mode": "read",
         "route": "C"}])
    move_scout(conn, scout_id, "synthesizing")
    record_scout_report(conn, scout_id, 1, REPORT)
    record_scout_actions(conn, scout_id, 1, ACTIONS[:1])
    record_scout_event(conn, scout_id, "scout_reported", "the lead reported")
    end_scout(conn, scout_id, "done", "the lead wrote its report")
    (ddl,) = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'scouts'").fetchone()
    indexes = conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name = 'scouts'"
        " AND type = 'index' AND sql IS NOT NULL").fetchall()
    old = ddl.replace("CHECK (kind IN ('bug', 'research', 'recon',"
                      " 'guardrails'))", OLD_KINDS)
    assert old != ddl, ddl
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(old.replace('"scouts"', "scouts_old", 1))
    conn.execute("INSERT INTO scouts_old SELECT * FROM scouts")
    conn.execute("DROP TABLE scouts")
    conn.execute("ALTER TABLE scouts_old RENAME TO scouts")
    for (sql,) in indexes:
        conn.execute(sql)
    conn.execute(f"PRAGMA user_version = {store.schema.SCHEMA_VERSION - 1:d}")
    conn.commit()
    conn.close()
    return scout_id


def references(conn, scout_id):
    return [conn.execute(f"SELECT count(*) FROM {table} WHERE scoutId = ?",
                         (scout_id,)).fetchone()[0]
            for table in ("scoutReports", "scoutTasks", "scoutActions",
                          "runEvents")]


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.db"

    def test_a_store_before_the_kind_keeps_its_scouts_and_takes_the_kind(self):
        scout_id = old_store(self.path)
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone(),
                         (store.schema.SCHEMA_VERSION,))
        row = scout(conn, scout_id)
        self.assertEqual((row.ask, row.kind, row.phase),
                         ("Map the merge gate", "recon", "done"))
        self.assertEqual(references(conn, scout_id), [1, 1, 1, 2])
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(),
                         [])
        (project,) = conn.execute("SELECT id FROM projects").fetchone()
        added = create_scout(conn, project, ASK, "guardrails", 20)
        self.assertEqual(scout(conn, added).kind, "guardrails")


def holo(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = holo_main(list(argv))
        except SystemExit as stop:
            code = stop.code
    return code, err.getvalue()


class LaunchTests(LaunchCase):
    def test_holo_scout_takes_kind_guardrails_and_refuses_triage(self):
        self.enterContext(patch.dict(os.environ, {"HOLO_TRANSPORT": "local"}))
        path = self.project("alpha")
        code, err = holo("scout", ASK, "--kind", "guardrails", "--json",
                         "-p", str(path))
        self.assertEqual(code, 0, err)
        (row,) = scout_rows(path)
        self.assertEqual((row.ask, row.kind), (ASK, "guardrails"))
        code, err = holo("scout", ASK, "--kind", "triage", "-p", str(path))
        self.assertEqual(code, 2)
        self.assertIn("argument --kind", err)
        self.assertEqual([each.id for each in scout_rows(path)], [row.id])


if __name__ == "__main__":
    unittest.main()
