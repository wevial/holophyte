"""`holo revert KEY REASON [--dry-run]`: the checks a revert passes, the
dependents it prints and the urgent ticket it files, against a real git
repository whose `origin/main` holds the merges and a native board's store.

Run: python3 -m unittest discover -s tests -p 'test_cli_revert*.py' -v
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
from phase_fixture import finish_run  # noqa: E402 - after the sys.path insert

import holophyte.cli.entry  # noqa: E402
import store  # noqa: E402
from holophyte.board import linear_provider  # noqa: E402
from holophyte.holo.cli import main as holo  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from holophyte.tickets import ticket_template  # noqa: E402
from tests.test_cli_native_update import body  # noqa: E402

NATIVE = '[board]\nkind = "native"\nprefix = "NAT"\n'
ENABLED = NATIVE + "[revert]\nenabled = true\n"
VERIFY = "python3 -m unittest discover -s tests -p 'test_thing.py'"


def no_linear(*args, **kwargs):
    raise AssertionError("the command line asked Linear")


class RevertCliTests(ConfigTestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "user.name=Tester", "-c", "user.email=t@example.org",
             *args], cwd=self.target, capture_output=True, text=True,
            check=True).stdout.strip()

    def commit(self, files, message):
        for path, text in files.items():
            (self.target / path).parent.mkdir(parents=True, exist_ok=True)
            (self.target / path).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def project_with(self, config, key_files, later_files=None,
                     key_outcome="merged"):
        """A repository whose NAT-1 merge changed `key_files` and whose
        NAT-2 merge after it changed `later_files`, both pushed to origin."""
        self.locate(config)
        self.git("init", "-q", "-b", "main")
        self.commit({"tests/test_thing.py": "", "src/header.py": "header\n",
                     "src/footer.py": "footer\n", "README.md": "readme\n"},
                    "base")
        for title in ("Header", "Footer"):
            path = self.root / f"{title}.md"
            path.write_text(body(title))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(holophyte.cli.entry.cli(
                    [str(self.target), "--file-ticket", str(path)]), 0)
        self.key_sha = self.commit(key_files, "NAT-1: header")
        self.later_sha = self.commit(later_files or {"README.md": "later\n"},
                                     "NAT-2: footer")
        self.git("init", "-q", "--bare", str(self.root / "origin.git"))
        self.git("remote", "add", "origin", str(self.root / "origin.git"))
        self.git("push", "-q", "origin", "main")
        self.runs = {}
        with contextlib.closing(open_store(self.project)) as conn:
            (project_id,) = conn.execute("SELECT id FROM projects").fetchone()
            for key, sha, outcome in (("NAT-1", self.key_sha, key_outcome),
                                      ("NAT-2", self.later_sha, "merged")):
                (ticket_id,) = conn.execute(
                    "SELECT id FROM tickets WHERE linearIdentifier = ?",
                    (key,)).fetchone()
                run = store.claim(conn, project_id, ticket_id)
                store.tickets.transition(conn, ticket_id, "in_flight")
                store.set_phase(conn, run, "working")
                finish_run(conn, run, outcome, merge_sha=sha
                           if outcome == "merged" else None)
                self.runs[key] = run

    def holo(self, *words):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = holo([*words, "-p", str(self.target)])
            except SystemExit as stopped:
                code = stopped.code
        return code, out.getvalue(), err.getvalue()

    def read(self, sql, *params):
        with contextlib.closing(sqlite3.connect(self.project.store_path)) as conn:
            return conn.execute(sql, params).fetchall()

    def tickets(self):
        return self.read("SELECT linearIdentifier FROM tickets ORDER BY id")

    def reverts(self):
        return self.read("SELECT runId, note FROM interventions"
                         " WHERE action = 'revert'")

    def test_revert_files_an_urgent_todo_ticket_and_records_its_intervention(self):
        self.project_with(ENABLED, {"src/header.py": "broken\n",
                                    "src/footer.py": "broken\n"})

        code, out, err = self.holo("revert", "NAT-1", "broke the header")

        self.assertEqual(code, 0, err)
        self.assertIn("revert ticket NAT-3 filed", out)
        ((title, text, priority, column, verify),) = self.read(
            "SELECT title, body, priority, boardColumn, verificationCommands"
            " FROM tickets WHERE linearIdentifier = 'NAT-3'")
        self.assertEqual(title, "[Revert] Undo NAT-1: Header")
        self.assertEqual((priority, column), (1, "ready"))
        self.assertIn(f"\nReverts: NAT-1 at {self.key_sha}\n", text)
        self.assertIn(f"`git revert --no-edit {self.key_sha}`", text)
        parsed = ticket_template.parse(text)
        self.assertEqual(parsed.in_scope, ["`src/footer.py`", "`src/header.py`"])
        self.assertEqual(parsed.verify_commands, [VERIFY])
        self.assertIn(VERIFY, verify)
        self.assertEqual(self.reverts(),
                         [(self.runs["NAT-1"], "filed NAT-3: broke the header")])
        filed = self.root / "filed.md"
        filed.write_text(text)
        with contextlib.redirect_stdout(io.StringIO()) as checked:
            self.assertEqual(ticket_template.main(
                ["--repo", str(self.target), str(filed)]), 0)
        self.assertIn("OK", checked.getvalue())

    def test_a_later_merge_on_a_shared_file_is_printed_as_a_dependent(self):
        self.project_with(ENABLED, {"src/header.py": "broken\n"},
                          {"src/header.py": "fixed\n", "README.md": "later\n"})
        dependent = f"dependent: NAT-2 {self.later_sha} src/header.py"

        code, out, err = self.holo("revert", "NAT-1", "broke the header",
                                   "--dry-run")

        self.assertEqual(code, 0, err)
        self.assertIn(dependent, out)
        self.assertNotIn("README.md", out.split("dependent:", 1)[1])
        self.assertEqual(self.tickets(), [("NAT-1",), ("NAT-2",)])
        self.assertEqual(self.reverts(), [])

        code, out, err = self.holo("revert", "NAT-1", "broke the header")

        self.assertEqual(code, 0, err)
        self.assertIn(dependent, out)
        self.assertIn("revert ticket NAT-3 filed", out)

    def test_an_unrelated_later_merge_prints_no_dependent(self):
        self.project_with(ENABLED, {"src/header.py": "broken\n"})

        code, out, err = self.holo("revert", "NAT-1", "broke the header",
                                   "--dry-run")

        self.assertEqual(code, 0, err)
        self.assertIn("dependent: none", out)

    def refused(self, *words):
        before = (self.tickets(), self.reverts())
        code, out, err = self.holo("revert", *words)
        self.assertEqual(code, 1, out + err)
        self.assertEqual((self.tickets(), self.reverts()), before)
        return err

    def test_a_project_without_revert_enabled_is_refused(self):
        self.project_with(NATIVE, {"src/header.py": "broken\n"})

        self.assertIn("NAT-1: [revert] enabled is not true",
                      self.refused("NAT-1", "broke the header"))

    def test_a_merge_touching_a_migrations_folder_is_refused(self):
        self.project_with(ENABLED, {"src/header.py": "broken\n",
                                    "db/migrations/0002_add.py": "op\n"})

        err = self.refused("NAT-1", "broke the header")

        self.assertIn("db/migrations/0002_add.py", err)
        self.assertIn("ship a forward fix instead", err)

    def test_a_ticket_whose_run_did_not_merge_is_refused(self):
        self.project_with(ENABLED, {"src/header.py": "broken\n"},
                          key_outcome="failed")

        self.assertIn("NAT-1: no run of it merged",
                      self.refused("NAT-1", "broke the header"))

    def test_a_ticket_with_an_open_revert_ticket_is_refused_naming_it(self):
        self.project_with(ENABLED, {"src/header.py": "broken\n"})
        self.assertEqual(self.holo("revert", "NAT-1", "broke the header")[0], 0)

        self.assertIn("NAT-1: NAT-3 is its open revert ticket",
                      self.refused("NAT-1", "still broken"))

    def test_startup_refuses_refuse_paths_that_are_not_a_list_of_strings(self):
        for value in ('"**/migrations/**"', "[1]"):
            with self.subTest(value=value):
                self.locate(f"{ENABLED}refuse_paths = {value}\n")
                with self.assertRaises(SystemExit) as raised, \
                        contextlib.redirect_stdout(io.StringIO()):
                    holophyte.cli.entry.cli([str(self.target)])
                self.assertIn("[revert] refuse_paths must be a list",
                              str(raised.exception.code))
