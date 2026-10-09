"""Filing runs the claim's landmark check against the target's real `main`,
and "new" exempts a backticked path from it only when it governs that path.

Run: python3 -m unittest discover -s tests -p 'test_filing_freshness.py' -v
"""
import contextlib
import io
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert

import holophyte.cli.entry  # noqa: E402
import linear_provider  # noqa: E402
from holophyte.board.projection import file_ticket  # noqa: E402
from holophyte.review.freshness import stale_reasons  # noqa: E402
from provider import FileProvider  # noqa: E402
from tests.test_cli_native_update import NATIVE, body, no_linear  # noqa: E402

TEMPLATE = Path(__file__).resolve().parents[1] / "ticket_template.py"
IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")
STALE_NOTES = "- Call `refuse()` in `src/thing.py`."
REASON = ("`refuse()` (named in Implementation notes #1) is not in"
          " `src/thing.py` on main")
ROW = "SELECT linearIdentifier, body, revision FROM tickets ORDER BY id"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def commit_main(repo, files):
    git(repo, "init", "-q", "-b", "main")
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, *IDENTITY, "commit", "-q", "-m", "fixture")


def ticket(title, notes="- None worth noting.", depends="none"):
    return body(title, depends).replace("- None worth noting.", notes)


class FilingFreshnessTests(ConfigTestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.locate(NATIVE)
        commit_main(self.target, {"tests/test_thing.py": "",
                                  "src/thing.py": "def accept():\n    pass\n"})
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.root)

    def cli(self, text, *args):
        Path("T.md").write_text(text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = holophyte.cli.entry.cli(
                [str(self.target), "--file-ticket", "T.md", *args])
        return status, out.getvalue()

    def rows(self):
        if not Path(self.project.store_path).exists():
            return []
        with contextlib.closing(sqlite3.connect(self.project.store_path)) as conn:
            return conn.execute(ROW).fetchall()

    def template(self, text):
        Path("T.md").write_text(text)
        return subprocess.run(
            [sys.executable, str(TEMPLATE), "--repo", str(self.target), "T.md"],
            capture_output=True, text=True)

    def test_a_stale_body_is_refused_and_the_next_clean_filing_is_the_first(self):
        status, printed = self.cli(ticket("Stale", STALE_NOTES))

        self.assertEqual((status, printed), (1, f"[holo2] T.md: {REASON}\n"))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.cli(ticket("Clean"))[0], 0)
        self.assertEqual([row[0] for row in self.rows()], ["NAT-1"])

    def test_an_update_to_a_stale_body_changes_nothing(self):
        self.assertEqual(self.cli(ticket("Clean"))[0], 0)
        before = self.rows()

        status, printed = self.cli(ticket("Stale", STALE_NOTES),
                                   "--update", "NAT-1", "--revision", "1")

        self.assertEqual((status, printed), (1, f"[holo2] T.md: {REASON}\n"))
        self.assertEqual(self.rows(), before)
        self.assertEqual(before[0][2], 1)

    def test_a_stale_body_with_a_dependency_files_with_a_warning(self):
        self.assertEqual(self.cli(ticket("Clean"))[0], 0)

        status, printed = self.cli(ticket("Waits", STALE_NOTES, "NAT-1"))

        self.assertEqual(status, 0)
        self.assertIn(f"[holo2] T.md: warning: {REASON}\n", printed)
        self.assertEqual([row[0] for row in self.rows()], ["NAT-1", "NAT-2"])

    def test_a_file_board_refuses_a_path_main_lacks_and_holds_no_ticket(self):
        boards = self.root / "board"
        boards.mkdir()
        Path("T.md").write_text(ticket("Gone", "- Rewrite `docs/gone.md`."))
        out = io.StringIO()

        status = file_ticket(self.project, "T.md", "Todo", FileProvider(boards),
                             out=out)

        self.assertEqual(status, 1)
        self.assertIn("`docs/gone.md` (named in Implementation notes) is not on"
                      " main", out.getvalue())
        self.assertEqual(list(boards.iterdir()), [])

    def test_the_template_checker_blocks_a_stale_body_and_advises_on_a_dependency(self):
        checked = self.template(ticket("Stale", STALE_NOTES))
        self.assertEqual(checked.returncode, 1, checked.stderr)
        self.assertEqual(checked.stdout.splitlines()[0], "T.md: INVALID")
        self.assertIn(f"  - {REASON}", checked.stdout.splitlines())

        checked = self.template(ticket("Waits", STALE_NOTES, "NAT-1"))
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(checked.stdout.splitlines()[0], "T.md: OK")
        self.assertIn(f"  - advisory: {REASON}", checked.stdout.splitlines())


class GoverningNewTests(ConfigTestCase):
    def setUp(self):
        self.locate()
        commit_main(self.target, {"tests/test_claims.py": "def claim():\n"
                                                          "    pass\n"})

    def reasons(self, notes):
        return stale_reasons(self.target, ticket("Notes", notes))

    def test_a_new_test_in_an_existing_file_still_pairs_its_symbol(self):
        self.assertEqual(self.reasons(
            "- Beside `refuse()`, add a new test in `tests/test_claims.py`."),
            ["`refuse()` (named in Implementation notes #1) is not in"
             " `tests/test_claims.py` on main"])

    def test_new_declares_only_the_paths_it_governs(self):
        self.assertEqual(self.reasons(
            "- Add a new test in `tests/test_gone.py`."),
            ["`tests/test_gone.py` (named in Implementation notes) is not on"
             " main"])
        for notes in ("- Add a new module `tests/test_gone.py`.",
                      "- Add a new test file `tests/test_gone.py`.",
                      "- Add new modules `tests/test_gone.py` and"
                      " `tests/test_other.py`."):
            with self.subTest(notes=notes):
                self.assertEqual(self.reasons(notes), [])

    def test_a_preposition_before_the_path_ends_the_declaration(self):
        for word in ("regarding", "concerning"):
            with self.subTest(word=word):
                self.assertEqual(self.reasons(
                    f"- Beside `refuse()`, add new tests {word}"
                    " `tests/test_claims.py`."),
                    ["`refuse()` (named in Implementation notes #1) is not in"
                     " `tests/test_claims.py` on main"])

    def test_a_list_entry_that_is_no_path_ends_the_declaration(self):
        self.assertEqual(self.reasons(
            "- Add a new helper `refuse()` and `tests/test_gone.py`."),
            ["`tests/test_gone.py` (named in Implementation notes) is not on"
             " main"])

    def test_every_directory_in_a_declared_list_covers_the_files_under_it(self):
        self.assertEqual(self.reasons(
            "- Add new directories `fixtures` and `outputs`, then write"
            " `outputs/report.py`."), [])
