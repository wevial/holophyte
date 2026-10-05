"""A native-board `--cancel` whose ticket has a live run closes the ticket
as `abandoned` once the run reaches its safe point, while an operator
`--abort` still parks it for the operator. The command line runs against a
real store under a throwaway home, and Linear's transport refuses to be
called.

Run: python3 -m unittest discover -s tests -p 'test_cancel_live_run.py' -v
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert

import holophyte.cli.entry  # noqa: E402
import linear_provider  # noqa: E402
import store  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from holophyte.loop.stop import stop_if_requested  # noqa: E402
from tests.test_cli_native_update import NATIVE, body, no_linear  # noqa: E402


class CancelLiveRunTests(ConfigTestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.locate(NATIVE)
        (self.target / "tests").mkdir()
        (self.target / "tests" / "test_thing.py").write_text("")
        path = self.root / "T.md"
        path.write_text(body("Thing"))
        self.assertEqual(self.cli("--file-ticket", str(path))[0], 0)

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = holophyte.cli.entry.cli([str(self.target), *args])
        return status, out.getvalue()

    def working_run(self, identifier="NAT-1"):
        """`identifier` claimed and working, as a worker leaves it between turns."""
        with contextlib.closing(open_store(self.project)) as conn:
            project_id, ticket_id = conn.execute(
                "SELECT projectId, id FROM tickets"
                " WHERE linearIdentifier = ?", (identifier,)).fetchone()
            run = store.claim(conn, project_id, ticket_id)
            store.tickets.transition(conn, ticket_id, "in_flight")
            store.set_phase(conn, run, "working")
        return run

    def reach_safe_point(self, run):
        with contextlib.closing(open_store(self.project)) as conn, \
                self.assertRaises(store.RunEnded) as ended:
            stop_if_requested(conn, run, "working")
        return ended.exception

    def read(self, sql):
        with contextlib.closing(sqlite3.connect(self.project.store_path)) as conn:
            return conn.execute(sql).fetchall()

    def test_a_cancel_closes_the_ticket_when_its_live_run_stops(self):
        run = self.working_run()
        status, printed = self.cli("--cancel", "NAT-1", "--revision", "1",
                                   "--note", "wrong scope")
        self.assertEqual(status, 0, printed)
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("in_flight",)])

        self.reach_safe_point(run)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("abandoned",)])
        self.assertEqual(self.read("SELECT status, blockedQuestion FROM tickets"),
                         [("abandoned", None)])
        self.assertEqual(
            self.read('SELECT source, "trigger" FROM interventions'
                      " WHERE action = 'abort'"),
            [("human", "board_cancelled")])
        status, printed = self.cli("--status")
        self.assertEqual(status, 0, printed)
        self.assertNotIn("parked NAT-1", printed)

    def test_a_cancel_after_a_pending_abort_still_closes_the_ticket(self):
        self.assertEqual(self.cli("--file-ticket", str(self.root / "T.md"))[0], 0)
        for identifier, abort, action in (("NAT-1", (), "abort"),
                                          ("NAT-2", ("--close-pr",), "abort_close")):
            with self.subTest(abort=abort):
                run = self.working_run(identifier)
                self.cli("--abort", identifier, "--note", "host going down", *abort)
                status, printed = self.cli("--cancel", identifier, "--revision",
                                           "1", "--note", "wrong scope")
                self.assertEqual(status, 0, printed)

                self.reach_safe_point(run)

                self.assertEqual(self.read(
                    "SELECT status, blockedQuestion FROM tickets"
                    f" WHERE linearIdentifier = '{identifier}'"),
                    [("abandoned", None)])
                self.assertEqual(self.read(
                    'SELECT i."action", i."trigger" FROM runs r'
                    " JOIN interventions i ON i.id = r.stopRequested"
                    f" WHERE r.id = {run}"), [(action, "board_cancelled")])
                printed = self.cli("--status")[1]
                self.assertNotIn(f"parked {identifier}", printed)

    def test_an_operator_abort_still_parks_the_ticket(self):
        run = self.working_run()
        printed = self.cli("--abort", "NAT-1", "--note", "host going down")[1]
        self.assertIn(f"run {run} ends at its worker's next heartbeat", printed)

        self.reach_safe_point(run)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("abandoned",)])
        self.assertEqual(self.read("SELECT status, blockedQuestion FROM tickets"),
                         [("blocked_on_operator", "host going down")])
        status, printed = self.cli("--status")
        self.assertEqual(status, 0, printed)
        self.assertIn(f"parked NAT-1 run {run}: host going down", printed)

    def test_a_cancel_with_no_live_run_abandons_the_ticket_at_once(self):
        self.assertEqual(
            self.cli("--cancel", "NAT-1", "--revision", "1", "--note", "dup"),
            (0, "[holo2] canceled NAT-1 (revision 2)\n"))
        self.assertEqual(self.read("SELECT status FROM tickets"), [("abandoned",)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM runs"), [(0,)])

    def test_a_version_40_store_admits_a_board_cancel_and_drops_version_40_builds(self):
        run = self.working_run()
        path = self.project.store_path
        with contextlib.closing(sqlite3.connect(path)) as old:
            (ddl,) = old.execute("SELECT sql FROM sqlite_master"
                                 " WHERE name = 'interventions'").fetchone()
            narrowed = ddl.replace(", 'board_cancelled'", "")
            self.assertNotIn("board_cancelled", narrowed)
            old.executescript(
                narrowed.replace("CREATE TABLE interventions (",
                                 "CREATE TABLE interventions_old (", 1)
                + ";\nINSERT INTO interventions_old SELECT * FROM interventions;\n"
                "DROP TABLE interventions;\n"
                "ALTER TABLE interventions_old RENAME TO interventions;\n"
                "PRAGMA user_version = 40;\n")

        status, printed = self.cli("--cancel", "NAT-1", "--revision", "1",
                                   "--note", "wrong scope")

        self.assertEqual(status, 0, printed)
        self.assertEqual(self.read("PRAGMA user_version"),
                         [(store.SCHEMA_VERSION,)])
        with contextlib.closing(sqlite3.connect(path)) as conn:
            note = json.loads(store.schema.latest_migration_note(conn))
        self.assertEqual(note["from"], 40)
        self.assertGreater(note["readableFrom"], 40,
                           "a version 40 build parks a board cancel")
        self.assertEqual(
            self.read('SELECT i."trigger" FROM runs r JOIN interventions i'
                      f" ON i.id = r.stopRequested WHERE r.id = {run}"),
            [("board_cancelled",)])
