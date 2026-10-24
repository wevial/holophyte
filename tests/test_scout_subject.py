"""A scout's subject: the store's two columns and their migration, the
`subject` of `POST /actions/scout` checked against the project's store and
served by `GET /scouts/ID`, `holo bug --about` and `holo scout show`
in-process, and the `holo mcp` `scout` tool's `subject` over stdio, the
scout sweep's spawn a recorder.

Run: python3 -m unittest discover -s tests -p 'test_scout_subject.py' -v
"""
import contextlib
import http.client
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema_fixture import DOCUMENTED_COLUMNS  # noqa: E402

import store  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.holo.cli import main as holo_main  # noqa: E402
from holophyte.host.supervisor import SWEEP_EVENT  # noqa: E402
from store.scouts import create_scout, move_scout, scout  # noqa: E402
from tests.test_holo_mcp import McpCase  # noqa: E402
from tests.test_scout_launch import (  # noqa: E402
    TOKEN,
    LaunchCase,
    first_event,
    scout_rows,
    slots,
)

ASK = "Why did it fail?"
PR = "https://github.com/acme/demo/pull/7"
SUBJECT_LINES = ("    subjectKind TEXT\n", "    subjectRef  TEXT\n")


def previous_schema():
    """`SCHEMA` without the subject columns, `totals` the last again."""
    lines = store.schema.SCHEMA.splitlines(keepends=True)
    start = lines.index(SUBJECT_LINES[0])
    end = lines.index(SUBJECT_LINES[1])
    lines[start - 1] = lines[start - 1].replace("TEXT,  -- JSON",
                                                "TEXT  -- JSON")
    return "".join(lines[:start] + lines[end + 1:])


class MigrationTests(unittest.TestCase):
    def test_a_store_one_version_back_gains_null_subjects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            conn = sqlite3.connect(path)
            conn.executescript(previous_schema())
            conn.execute(store.schema._INTERVENTIONS_DDL)
            self.assertNotIn("subjectKind", {row[1] for row in conn.execute(
                "PRAGMA table_info(scouts)")})
            project = store.ensure_project(conn, "team", "/repo")
            conn.execute(
                "INSERT INTO scouts (projectId, ask, kind, budgetMin, phase,"
                " createdAt) VALUES (?, 'where is it?', 'bug', 30, 'queued',"
                " 1)", (project,))
            conn.execute(
                f"PRAGMA user_version = {store.schema.SCHEMA_VERSION - 1}")
            conn.commit()
            conn.close()

            conn = store.open(path)
            self.addCleanup(conn.close)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone(),
                             (store.schema.SCHEMA_VERSION,))
            self.assertEqual(store.schema.READABLE_FROM, 50)
            row = scout(conn, 1)
            self.assertEqual((row.ask, row.phase, row.subject_kind,
                              row.subject_ref),
                             ("where is it?", "queued", None, None))
            tables = {name for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%'")}
            self.assertEqual(tables, set(DOCUMENTED_COLUMNS))
            for table, expected in DOCUMENTED_COLUMNS.items():
                with self.subTest(table=table):
                    self.assertEqual({row[1] for row in conn.execute(
                        f'PRAGMA table_info("{table}")')}, expected)


def mirror(conn, project, key):
    return store.tickets.mirror_ticket(
        conn, project, linear_issue_id=f"issue-{key}", linear_identifier=key,
        title=f"ticket {key}", acceptance_criteria=[f"Given {key}, it works"],
        verification_commands=["echo ok"], time_box_ms=1_500_000,
        board_column="ready")


def failed_run(conn, project, key):
    ticket = mirror(conn, project, key)
    store.tickets.transition(conn, ticket, "in_flight")
    run = store.claim(conn, project, ticket)
    store.release(conn, run, "failed", reason="verify went red")
    return run


class SubjectCase(LaunchCase):
    """Project `alpha` holding failed run R, run D stopped by a
    contradiction, ticket NAT-3 with no run, and run P that opened PR."""

    def setUp(self):
        super().setUp()
        self.path = self.project("alpha")
        conn = self.conn(self.path)
        (project,) = conn.execute("SELECT id FROM projects").fetchone()
        self.run_r = failed_run(conn, project, "NAT-1")
        self.run_d = failed_run(conn, project, "NAT-2")
        store.record_event(conn, self.run_d, "contradiction",
                           "C2 contradicts C1", level="detail",
                           payload=json.dumps({"criteria": []}))
        mirror(conn, project, "NAT-3")
        self.run_p = failed_run(conn, project, "NAT-4")
        store.writes.set_pull_request(conn, self.run_p, PR)
        self.server = self.serve(self.path)

    def launch(self, **extra):
        return self.post(self.server, "/actions/scout",
                         {"ask": ASK, "kind": "bug", **extra})

    def get(self, path):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=30)
        try:
            connection.request("GET", path,
                               headers={"Authorization": f"Bearer {TOKEN}"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def subject_of(self, scout_id):
        return self.conn(self.path).execute(
            "SELECT subjectKind, subjectRef FROM scouts WHERE id = ?",
            (scout_id,)).fetchone()


class DaemonSubjectTests(SubjectCase):
    def test_a_run_subject_is_stored_and_served_and_none_is_null(self):
        code, reply = self.launch(subject=f"run:{self.run_r}")
        self.assertEqual(code, 200, reply)
        self.assertIs(reply["ok"], True)
        self.assertEqual(self.subject_of(reply["scout"]),
                         ("run", str(self.run_r)))
        code, body = self.get(f"/scouts/{reply['scout']}")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["scout"]["subject"],
                         {"kind": "run", "ref": str(self.run_r)})

        code, reply = self.launch()
        self.assertEqual(code, 200, reply)
        self.assertEqual(self.subject_of(reply["scout"]), (None, None))
        code, body = self.get(f"/scouts/{reply['scout']}")
        self.assertIsNone(body["scout"]["subject"])

    def test_a_defect_a_ticket_and_a_pr_are_stored_with_their_event(self):
        for subject, stored in (
                (f"defect:{self.run_d}", ("defect", str(self.run_d))),
                ("ticket:NAT-3", ("ticket", "NAT-3")),
                (f"pr:{PR}", ("pr", PR))):
            with self.subTest(subject=subject):
                code, reply = self.launch(subject=subject)
                self.assertEqual(code, 200, reply)
                self.assertIs(reply["ok"], True, reply)
                self.assertEqual(self.subject_of(reply["scout"]), stored)
                kind, payload = first_event(self.path, reply["scout"])
                self.assertEqual((kind, payload["subject"]),
                                 ("scout_queued", subject))

    def test_a_sweep_stop_is_a_defect_only_when_the_sweep_failed_the_run(self):
        conn = self.conn(self.path)
        (project,) = conn.execute("SELECT id FROM projects").fetchone()
        acted, held = (failed_run(conn, project, key)
                       for key in ("NAT-5", "NAT-6"))
        store.record_event(conn, acted, SWEEP_EVENT,
                           "supervisor sweep: review_stuck (3 rounds);"
                           " failing the run and releasing its leases")
        store.record_event(conn, held, SWEEP_EVENT,
                           "supervisor sweep: review_stuck (3 rounds) no"
                           " longer held at re-check; run is now merging;"
                           " no action")
        code, reply = self.launch(subject=f"defect:{acted}")
        self.assertIs(reply["ok"], True, reply)
        self.assertEqual(self.subject_of(reply["scout"]),
                         ("defect", str(acted)))
        code, reply = self.launch(subject=f"defect:{held}")
        self.assertEqual((code, reply["ok"]), (200, False), reply)


class RefusedSubjectTests(SubjectCase):
    def test_a_malformed_subject_is_400_naming_it_and_writes_nothing(self):
        for subject in ("run", "run:abc", "run:0", "story:1", "pr:ftp://x",
                        "ticket:", 12):
            with self.subTest(subject=subject):
                code, reply = self.launch(subject=subject)
                self.assertEqual(code, 400, reply)
                self.assertIn("subject", reply["error"])
        self.assertEqual(scout_rows(self.path), [])
        self.assertEqual(self.spawned, [])

    def test_a_subject_the_store_does_not_hold_is_declined(self):
        for subject, ref in (("run:999", "999"), ("run:" + "9" * 5000, "999"),
                             (f"defect:{self.run_r}", str(self.run_r)),
                             ("ticket:NAT-9", "NAT-9"),
                             ("pr:https://github.com/acme/demo/pull/99",
                              "https://github.com/acme/demo/pull/99")):
            with self.subTest(subject=subject):
                code, reply = self.launch(subject=subject)
                self.assertEqual(code, 200, reply)
                self.assertIs(reply["ok"], False)
                self.assertIn(ref, reply["detail"])
        self.assertEqual(scout_rows(self.path), [])
        self.assertEqual(self.spawned, [])


class HoloSubjectTests(SubjectCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {"HOLO_TRANSPORT": "local"}))

    def holo(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = holo_main(list(argv))
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_holo_bug_about_records_the_subject_and_scout_show_prints_it(self):
        code, out, err = self.holo("bug", ASK, "--about", f"run:{self.run_r}",
                                   "--json", "-p", str(self.path))
        self.assertEqual(code, 0, err)
        (row,) = scout_rows(self.path)
        self.assertEqual((row.subject_kind, row.subject_ref),
                         ("run", str(self.run_r)))
        code, out, err = self.holo("scout", "show", str(row.id),
                                   "-p", str(self.path))
        self.assertEqual(code, 0, err)
        self.assertIn(f"about run:{self.run_r}", out.splitlines())


class McpSubjectTests(McpCase):
    def test_the_scout_tool_takes_an_optional_subject(self):
        conn = store.open(str(self.store_path))
        try:
            (project,) = conn.execute("SELECT id FROM projects").fetchone()
            self.ticket(conn, project, "NAT-3")
            live = create_scout(conn, project, "a live scout", "recon", 30)
            move_scout(conn, live, "planning", base_sha="a" * 40)
        finally:
            conn.close()
        slots(1)

        async def use(client, _):
            return (await client.list_tools()).tools
        (tool,) = [tool for tool in self.session(use) if tool.name == "scout"]
        self.assertIn("subject", tool.input_schema["properties"])
        self.assertNotIn("subject", tool.input_schema["required"])

        result = self.call("scout", {"project": "alpha", "note": ASK,
                                     "kind": "bug", "author": "test seat",
                                     "subject": "ticket:NAT-3"})
        self.assertIs(result.is_error, False, result.content)
        row = scout_rows(self.alpha)[-1]
        self.assertEqual((row.phase, row.subject_kind, row.subject_ref),
                         ("queued", "ticket", "NAT-3"))


if __name__ == "__main__":
    unittest.main()
