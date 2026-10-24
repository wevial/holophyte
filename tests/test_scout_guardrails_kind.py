"""The `guardrails` scout kind: a store at the version before it carries its
scouts over, the store accepts the kind, `POST /actions/scout`, `holo scout`
and the `holo mcp` `scout` tool launch it, a run helper of it only reads, and
`GET /scouts` and `GET /scouts/ID` serve it.

Run: python3 -m unittest discover -s tests -p 'test_scout_guardrails_kind.py' -v
"""
import contextlib
import http.client
import io
import json
import os
import sqlite3
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
from tests.host_fixture import git
from tests.scout_stubs import fenced
from tests.test_holo_mcp import McpCase
from tests.test_scout_launch import TOKEN, LaunchCase, scout_rows, slots
from tests.test_scout_lead import REPORT as LEAD_REPORT
from tests.test_scout_run_helpers import LOOK, READ_ONLY, SETUP_RAN, plan
from tests.test_serve_scouts import ACTIONS, REPORT

ASK = "Mine the findings"
RUN_Q = "Which review findings recur?"
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
        readable_from = store.schema.READABLE_FROM
        before = sqlite3.connect(self.path)
        (ddl,) = before.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'scouts'").fetchone()
        before.close()
        self.assertIn(OLD_KINDS, ddl)

        conn = store.open(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone(),
                         (store.schema.SCHEMA_VERSION,))
        self.assertEqual(store.schema.READABLE_FROM, readable_from)
        row = scout(conn, scout_id)
        self.assertEqual((row.ask, row.kind, row.phase),
                         ("Map the merge gate", "recon", "done"))
        self.assertEqual(references(conn, scout_id), [1, 1, 1, 2])
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(),
                         [])
        (project,) = conn.execute("SELECT id FROM projects").fetchone()
        added = create_scout(conn, project, ASK, "guardrails", 20)
        self.assertEqual(scout(conn, added).kind, "guardrails")

    def test_a_fresh_store_records_guardrails_and_refuses_guardrail(self):
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        project = ensure_project(conn, "native:ALPHA", "/repo")
        scout_id = create_scout(conn, project, ASK, "guardrails", 20)
        self.assertEqual(scout(conn, scout_id).kind, "guardrails")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO scouts (projectId, ask, kind, budgetMin,"
                         " phase, createdAt) VALUES (?, ?, 'guardrail', 20,"
                         " 'queued', 1)", (project, ASK))


def holo(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = holo_main(list(argv))
        except SystemExit as stop:
            code = stop.code
    return code, err.getvalue()


class LaunchTests(LaunchCase):
    def test_the_daemon_launches_guardrails_and_refuses_guardrail(self):
        path = self.project("alpha")
        server = self.serve(path)
        code, reply = self.post(server, "/actions/scout",
                                {"ask": ASK, "kind": "guardrails"})
        self.assertEqual(code, 200, reply)
        self.assertIs(reply["ok"], True)
        (row,) = scout_rows(path)
        self.assertEqual((row.id, row.ask, row.kind),
                         (reply["scout"], ASK, "guardrails"))
        code, reply = self.post(server, "/actions/scout",
                                {"ask": ASK, "kind": "guardrail"})
        self.assertEqual(code, 400, reply)
        self.assertIn("kind", reply["error"])
        self.assertIn("guardrails", reply["error"])
        self.assertEqual([row.id for row in scout_rows(path)], [row.id])

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


class McpTests(McpCase):
    def test_the_scout_tool_lists_and_queues_guardrails(self):
        conn = store.open(str(self.store_path))
        try:
            (project,) = conn.execute("SELECT id FROM projects").fetchone()
            live = create_scout(conn, project, "a live scout", "recon", 30)
            move_scout(conn, live, "planning", base_sha="a" * 40)
        finally:
            conn.close()
        slots(1)

        async def use(client, _):
            return (await client.list_tools()).tools
        (tool,) = [tool for tool in self.session(use) if tool.name == "scout"]
        self.assertEqual(
            set(tool.input_schema["properties"]["kind"]["enum"]),
            {"bug", "research", "recon", "guardrails"})

        result = self.call("scout", {"project": "alpha", "note": ASK,
                                     "kind": "guardrails",
                                     "author": "test seat"})
        self.assertIs(result.is_error, False, result.content)
        conn = store.open(str(self.store_path))
        try:
            row = scout(conn, conn.execute(
                "SELECT max(id) FROM scouts").fetchone()[0])
        finally:
            conn.close()
        self.assertEqual((row.ask, row.kind, row.phase),
                         (ASK, "guardrails", "queued"))


class RunTests(LaunchCase):
    def run_guardrails(self):
        path = self.project("alpha", SETUP_RAN)
        info = Path(git(path, "rev-parse", "--path-format=absolute",
                        "--git-common-dir")) / "info"
        info.mkdir(exist_ok=True)
        (info / "exclude").write_text("setup-ran\n")
        conn = self.conn(path)
        (project,) = conn.execute("SELECT id FROM projects").fetchone()
        scout_id = create_scout(conn, project, ASK, "guardrails", 30)
        move_scout(conn, scout_id, "planning",
                   base_sha=git(path, "rev-parse", "main"))
        self.stubs.answer(plan=plan((RUN_Q, "run")),
                          report=fenced(LEAD_REPORT), helper_sh=[LOOK])
        code, printed = self.cli("--scout", str(scout_id), str(path))
        self.assertEqual(code, 0, printed)
        return path, scout_id

    def get(self, server, route):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=30)
        try:
            connection.request("GET", route,
                               headers={"Authorization": f"Bearer {TOKEN}"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_a_guardrails_scout_runs_its_run_task_as_a_read_task(self):
        self.run_guardrails()
        (helper,) = [helper for helper in self.stubs.helpers()
                     if helper["question"] == RUN_Q]
        self.assertEqual(helper["sh"], [[0, "absent"]])
        (turn,) = self.stubs.turns("helper")
        self.assertIn(READ_ONLY, turn["argv"][-1])

    def test_the_scout_reads_serve_the_done_guardrails_scout(self):
        path, scout_id = self.run_guardrails()
        self.assertEqual(scout(self.conn(path), scout_id).phase, "done")
        server = self.serve(path)
        code, listed = self.get(server, "/scouts")
        self.assertEqual(code, 200, listed)
        self.assertEqual([(row["id"], row["kind"]) for row in listed["scouts"]],
                         [(scout_id, "guardrails")])
        code, detail = self.get(server, f"/scouts/{scout_id}")
        self.assertEqual(code, 200, detail)
        self.assertEqual((detail["scout"]["id"], detail["scout"]["kind"]),
                         (scout_id, "guardrails"))


if __name__ == "__main__":
    unittest.main()
