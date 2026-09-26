"""KO-760: `--file-ticket --update KEY-n` on a native board takes
`--revision N`, `--priority` and `--labels`, and a refusal of the store's is
one printed line with exit 1; on a Linear board the three are usage errors
beside `--update`. The command line runs against a real store under a
throwaway home, and Linear's transport refuses to be called.

Run: python3 -m unittest discover -s tests -p 'test_cli_native_update.py' -v
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
from tests.test_provider import ticket_body  # noqa: E402

NATIVE = '[board]\nkind = "native"\nkey = "NAT"\n'
LINEAR = '[board]\nproject_id = "p-1"\nteam = "T"\n'
ROW = ("SELECT linearIdentifier, title, body, priority, labels, revision"
       " FROM tickets ORDER BY linearIdentifier")


def body(title, depends="none"):
    """A body filing takes, its verify line one module of the repository."""
    return ticket_body(title=title, verify=(
        "python3 -m unittest discover -s tests -p 'test_thing.py'")).replace(
        "Depends on: none", f"Depends on: {depends}")


def no_linear(*args, **kwargs):
    raise AssertionError("the command line asked Linear")


class NativeUpdateCliTests(ConfigTestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def native(self):
        """A native project holding NAT-1, filed from `T1.md`."""
        self.locate(NATIVE)
        (self.target / "tests").mkdir()
        (self.target / "tests" / "test_thing.py").write_text("")
        self.assertEqual(self.cli(self.ticket("T1.md", body("First")))[0], 0)

    def ticket(self, name, text):
        path = self.root / name
        path.write_text(text)
        return path

    def cli(self, path, *args):
        """Run `--file-ticket path args` on the project; status and stdout."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = holophyte.cli.cli(
                [str(self.target), "--file-ticket", str(path), *args])
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

    def rows(self):
        with contextlib.closing(sqlite3.connect(self.project.store_path)) as conn:
            return conn.execute(ROW).fetchall()

    def test_an_update_at_the_read_revision_lands_and_a_stale_one_changes_nothing(self):
        self.native()
        self.assertEqual(self.rows()[0][5], 1)
        t2 = self.ticket("T2.md", body("Second"))

        status, printed = self.cli(t2, "--update", "NAT-1", "--revision", "1",
                                   "--priority", "high", "--labels", "ui,api")

        self.assertEqual((status, printed), (0, "[holo2] updated NAT-1: Second\n"))
        (row,) = self.rows()
        self.assertEqual(row[1:4], ("Second", body("Second"), 2))
        self.assertEqual(row[4], '["ui", "api"]')
        self.assertEqual(row[5], 2)

        status, printed = self.cli(t2, "--update", "NAT-1", "--revision", "1",
                                   "--priority", "low")

        self.assertEqual((status, printed), (1, f"[holo2] {t2}: NAT-1 is at "
                                             "revision 2, not 1; nothing changed\n"))
        self.assertEqual(self.rows(), [row])

    def test_a_missing_revision_and_an_unknown_dependency_are_one_line_each(self):
        self.native()
        before = self.rows()

        status, printed = self.cli(self.ticket("T2.md", body("Second")),
                                   "--update", "NAT-1")
        self.assertEqual(status, 1)
        self.assertEqual(len(printed.splitlines()), 1)
        self.assertIn("--revision", printed)

        status, printed = self.cli(
            self.ticket("T3.md", body("Third", depends="NAT-9")))
        self.assertEqual(status, 1)
        self.assertEqual(len(printed.splitlines()), 1)
        self.assertIn("NAT-9", printed)
        self.assertEqual(self.rows(), before)

    def test_on_a_linear_board_revision_and_labels_beside_update_are_usage_errors(self):
        self.locate(LINEAR)
        t = self.ticket("T.md", body("Linear"))
        for option in (("--revision", "3"), ("--labels", "ui")):
            with self.subTest(option=option[0]):
                err = self.usage_error("--file-ticket", str(t),
                                       "--update", "KO-7000", *option)
                self.assertIn(option[0], err)
