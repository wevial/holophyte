"""KO-764: `--move KEY-n ready|backlog` and `--cancel KEY-n` move and
cancel a native ticket at `--revision N`, a cancel reaching a live run; on
a Linear board both are usage errors that ask Linear nothing. The command
line runs against a real store under a throwaway home, and Linear's
transport refuses to be called.

Run: python3 -m unittest discover -s tests -p 'test_cli_native_move.py' -v
"""
import contextlib
import io
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert

import holophyte.cli  # noqa: E402
import linear_provider  # noqa: E402
import store  # noqa: E402
import store.board  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.runs import open_store  # noqa: E402
from tests.test_cli_native_update import LINEAR, NATIVE, body  # noqa: E402


def no_linear(*args, **kwargs):
    raise AssertionError("the command line asked Linear")


class NativeMoveCliTests(ConfigTestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def native(self, tickets):
        """A native project holding `tickets` tickets, NAT-1 onward, filed
        through the command line into Ready at revision 1."""
        self.locate(NATIVE)
        (self.target / "tests").mkdir()
        (self.target / "tests" / "test_thing.py").write_text("")
        path = self.root / "T.md"
        path.write_text(body("Thing"))
        for _ in range(tickets):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(holophyte.cli.cli(
                    [str(self.target), "--file-ticket", str(path)]), 0)

    def cli(self, *args):
        """Run the command line on the project; its status and stdout."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = holophyte.cli.cli([str(self.target), *args])
        return status, out.getvalue()

    def usage_error(self, *args):
        """Run a command line argparse refuses; its stderr."""
        err = io.StringIO()
        with self.assertRaises(SystemExit) as raised, \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            holophyte.cli.cli([str(self.target), *args])
        self.assertEqual(raised.exception.code, 2)
        return err.getvalue()

    def ticket(self, identifier):
        with contextlib.closing(sqlite3.connect(self.project.store_path)) as conn:
            return conn.execute(
                "SELECT boardColumn, revision, status FROM tickets"
                " WHERE linearIdentifier = ?", (identifier,)).fetchone()

    def test_a_move_at_the_read_revision_lands_and_a_stale_one_changes_nothing(self):
        self.native(1)
        self.assertEqual(self.ticket("NAT-1"), ("ready", 1, "ready"))

        self.assertEqual(self.cli("--move", "NAT-1", "backlog", "--revision", "1"),
                         (0, "[holo2] moved NAT-1 to backlog (revision 2)\n"))
        self.assertEqual(self.ticket("NAT-1")[:2], ("backlog", 2))

        self.assertEqual(self.cli("--move", "NAT-1", "ready", "--revision", "1"),
                         (1, "[holo2] NAT-1 is at revision 2, not 1; "
                             "nothing changed\n"))
        self.assertEqual(self.ticket("NAT-1")[:2], ("backlog", 2))

    def test_a_cancel_aborts_the_live_run_and_requires_a_note(self):
        self.native(8)
        conn = open_store(self.project)
        with contextlib.closing(conn):
            project_id = conn.execute("SELECT id FROM projects").fetchone()[0]
            store.board.move_ticket(conn, project_id, "NAT-2", "backlog", 1)
            store.board.move_ticket(conn, project_id, "NAT-2", "ready", 2)
            runs = []
            for n in (3, 4, 5, 6, 7, 8, 2):
                (ticket_id,) = conn.execute(
                    "SELECT id FROM tickets WHERE linearIdentifier = ?",
                    (f"NAT-{n}",)).fetchone()
                runs.append(store.claim(conn, project_id, ticket_id))
                store.tickets.transition(conn, ticket_id, "in_flight")
            store.set_phase(conn, runs[-1], "working")
        self.assertEqual(runs[-1], 7)
        self.assertEqual(self.ticket("NAT-2")[:2], ("ready", 3))

        status, printed = self.cli("--cancel", "NAT-2", "--revision", "3",
                                   "--note", "wrong scope")

        self.assertEqual((status, printed), (
            0, "[holo2] canceled NAT-2 (revision 4); run 7 ends abandoned at"
               " its next safe point\n"))
        self.assertEqual(self.ticket("NAT-2"), ("canceled", 4, "in_flight"))
        with contextlib.closing(sqlite3.connect(self.project.store_path)) as conn:
            stop = conn.execute(
                'SELECT i."action", i.guidance, r.endedAt FROM runs r'
                " JOIN interventions i ON i.id = r.stopRequested"
                " WHERE r.id = 7").fetchone()
        self.assertEqual(stop, ("abort", "wrong scope", None))

        self.assertIn("--note", self.usage_error("--cancel", "NAT-2",
                                                 "--revision", "4"))

    def test_a_cancel_with_no_live_run_names_none(self):
        self.native(1)
        self.assertEqual(
            self.cli("--cancel", "NAT-1", "--revision", "1", "--note", "dup"),
            (0, "[holo2] canceled NAT-1 (revision 2)\n"))
        self.assertEqual(self.ticket("NAT-1"), ("canceled", 2, "abandoned"))

    def test_on_a_linear_board_a_move_is_a_usage_error_naming_the_native_board(self):
        self.locate(LINEAR)
        err = self.usage_error("--move", "KO-1", "ready", "--revision", "1")
        self.assertIn("native", err)
        self.assertIn("--move", err)

    def test_a_move_needs_a_revision_and_a_column_it_can_take(self):
        self.native(1)
        self.assertIn("--revision", self.usage_error("--move", "NAT-1", "ready"))
        self.assertIn("ready or backlog",
                      self.usage_error("--move", "NAT-1", "done",
                                       "--revision", "1"))
        self.assertEqual(self.ticket("NAT-1"), ("ready", 1, "ready"))
