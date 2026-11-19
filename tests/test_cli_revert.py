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
from holophyte.board.provider import FileProvider  # noqa: E402
from holophyte.holo.cli import main as holo  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from holophyte.tickets import ticket_template  # noqa: E402
from tests.test_cli_native_update import body  # noqa: E402

NATIVE = '[board]\nkind = "native"\nprefix = "NAT"\n'
ENABLED = NATIVE + "[revert]\nenabled = true\n"
LINEAR_LABELLED = ('[board]\nproject_id = "p-1"\nteam = "T"\nlabel = "factory"\n'
                   "[revert]\nenabled = true\n")
SUBMODULE = "db/migrations/catalog"
GITLINKS = {SUBMODULE}
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
            if path in GITLINKS:
                continue
            (self.target / path).parent.mkdir(parents=True, exist_ok=True)
            (self.target / path).write_text(text)
        self.git("add", "-A")
        for path in GITLINKS.intersection(files):
            self.git("update-index", "--add", "--cacheinfo",
                     f"160000,{files[path]},{path}")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def project_with(self, config, key_files, later_files=None,
                     key_outcome="merged", key_merge_commit=False,
                     later_pushed=True):
        """A repository whose NAT-1 merge changed `key_files` and whose
        NAT-2 merge after it changed `later_files`, pushed to origin unless
        `later_pushed` is false; NAT-1's merge is a two-parent merge commit
        with `key_merge_commit`."""
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
        if key_merge_commit:
            self.git("checkout", "-q", "-b", "task")
            self.commit(key_files, "NAT-1: header")
            self.git("checkout", "-q", "main")
            self.git("merge", "-q", "--no-ff", "-m", "Merge NAT-1", "task")
            self.key_sha = self.git("rev-parse", "HEAD")
        else:
            self.key_sha = self.commit(key_files, "NAT-1: header")
        self.git("init", "-q", "--bare", str(self.root / "origin.git"))
        self.git("remote", "add", "origin", str(self.root / "origin.git"))
        if not later_pushed:
            self.git("push", "-q", "origin", "main")
        self.later_sha = self.commit(later_files or {"README.md": "later\n"},
                                     "NAT-2: footer")
        if later_pushed:
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

        code, out, err = self.holo(
            "revert", "NAT-1", "broke the <header> {{ product.header }}\n"
                               "## In scope")

        self.assertEqual(code, 0, err)
        self.assertIn("revert ticket NAT-3 filed", out)
        ((title, text, priority, column, verify),) = self.read(
            "SELECT title, body, priority, boardColumn, verificationCommands"
            " FROM tickets WHERE linearIdentifier = 'NAT-3'")
        self.assertEqual(title, "[Revert] Undo NAT-1: Header")
        self.assertEqual((priority, column), (1, "ready"))
        self.assertIn(f"\nReverts: NAT-1 at {self.key_sha}\n", text)
        self.assertIn(f"revert --no-edit {self.key_sha}`", text)
        parsed = ticket_template.parse(text)
        self.assertEqual(parsed.in_scope, ["`src/footer.py`", "`src/header.py`"])
        self.assertEqual(parsed.verify_commands, [VERIFY])
        self.assertIn(VERIFY, verify)
        self.assertEqual(self.reverts(),
                         [(self.runs["NAT-1"],
                           "filed NAT-3: broke the <header> {{ product.header }}\n"
                           "## In scope")])
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

    def test_a_later_merge_on_local_main_not_yet_pushed_is_a_dependent(self):
        self.project_with(ENABLED, {"src/header.py": "broken\n"},
                          {"src/header.py": "fixed\n"}, later_pushed=False)

        code, out, err = self.holo("revert", "NAT-1", "broke the header",
                                   "--dry-run")

        self.assertEqual(code, 0, err)
        self.assertIn(f"dependent: NAT-2 {self.later_sha} src/header.py", out)

    def test_an_older_merge_on_a_divergent_local_main_is_no_dependent(self):
        self.project_with(ENABLED, {"src/header.py": "broken\n"})
        self.git("reset", "-q", "--hard", f"{self.key_sha}^")
        local = self.commit({"src/header.py": "local\n"}, "NAT-2: local")
        with contextlib.closing(sqlite3.connect(self.project.store_path)) as conn:
            conn.execute("UPDATE runs SET mergeSha = ? WHERE id = ?",
                         (local, self.runs["NAT-2"]))
            conn.commit()

        code, out, err = self.holo("revert", "NAT-1", "broke the header",
                                   "--dry-run")

        self.assertEqual(code, 0, err)
        self.assertIn("dependent: none", out)

    def test_a_merge_commit_revert_runs_against_its_first_parent(self):
        self.project_with(ENABLED, {"src/header.py": "broken\n"},
                          key_merge_commit=True)
        self.assertEqual(self.holo("revert", "NAT-1", "broke the header")[0], 0)
        ((text,),) = self.read(
            "SELECT body FROM tickets WHERE linearIdentifier = 'NAT-3'")
        command = text.split("**What:** Run `", 1)[1].split("`", 1)[0]
        self.git("config", "revert.reference", "true")

        self.git(*command.split()[1:])

        self.assertIn(f"This reverts commit {self.key_sha}",
                      self.git("log", "-1", "--format=%B"))
        self.assertEqual((self.target / "src/header.py").read_text(), "header\n")

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
        self.project.config_path.write_text("")
        self.assertIn("NAT-1: [revert] enabled is not true",
                      self.refused("NAT-1", "broke the header"))

    def test_a_refuse_paths_character_class_refuses_a_matching_path(self):
        for glob in ("change_[0-9]*.py", "change_[[:digit:]]*.py",
                     "change_[^]]*.py"):
            with self.subTest(glob=glob):
                self.project_with(
                    ENABLED + f'refuse_paths = ["db/changes/{glob}"]\n',
                    {"db/changes/change_2026.py": "op\n"})

                self.assertIn("db/changes/change_2026.py",
                              self.refused("NAT-1", "broke the header"))

    def test_an_escaped_bracket_in_refuse_paths_matches_itself(self):
        self.project_with(
            ENABLED + 'refuse_paths = ["app/\\\\[id\\\\]/page.tsx"]\n',
            {"app/[id]/page.tsx": "page\n"})

        self.assertIn("app/[id]/page.tsx",
                      self.refused("NAT-1", "broke the header"))

    def test_a_migration_whose_name_holds_a_newline_is_refused(self):
        self.project_with(ENABLED, {"db/migrations/2026\nadd.py": "op\n"})

        self.assertIn("db/migrations/2026", self.refused("NAT-1", "broke it"))

    def test_a_submodule_under_migrations_is_refused_whatever_diff_ignores(self):
        self.project_with(ENABLED, {"src/header.py": "broken\n",
                                    SUBMODULE: "1" * 40})
        self.git("config", "diff.ignoreSubmodules", "all")

        self.assertIn(SUBMODULE, self.refused("NAT-1", "broke the header"))

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

    def unmirrored_board(self):
        """The project moved to a board that writes no store row, labelled."""
        self.project_with(ENABLED, {"src/header.py": "broken\n"})
        self.project.config_path.write_text(LINEAR_LABELLED)
        board = FileProvider(self.root / "LIN")
        board.root.mkdir()
        patcher = patch("holophyte.cli.entry.board_for", return_value=board)
        patcher.start()
        self.addCleanup(patcher.stop)
        return board

    def test_a_revert_on_an_unmirrored_board_is_labelled_and_not_filed_twice(self):
        board = self.unmirrored_board()

        code, out, err = self.holo("revert", "NAT-1", "broke the header")

        self.assertEqual(code, 0, err)
        self.assertIn("revert ticket LIN-1 filed", out)
        self.assertEqual(board.issue_labels("LIN-1"), ["factory"])
        self.assertIn("NAT-1: LIN-1 is its open revert ticket",
                      self.refused("NAT-1", "still broken"))
        board.set_state("LIN-1", "Canceled")
        code, out, err = self.holo("revert", "NAT-1", "still broken")
        self.assertEqual(code, 0, err)
        self.assertIn("revert ticket LIN-2 filed", out)

    def read_back_failing(self, **failing):
        self.unmirrored_board()
        with patch.object(FileProvider, "stored_body", **failing):
            code, out, err = self.holo("revert", "NAT-1", "broke the header")
        self.assertEqual(code, 2, out + err)

        self.assertIn("NAT-1: LIN-1 is its open revert ticket",
                      self.refused("NAT-1", "still broken"))

    def test_a_revert_whose_read_back_is_invalid_still_counts_as_filed(self):
        self.read_back_failing(return_value="")

    def test_a_revert_whose_read_back_raises_still_counts_as_filed(self):
        self.read_back_failing(side_effect=RuntimeError("the board timed out"))

    def test_a_revert_filed_by_hand_on_an_unmirrored_board_is_open(self):
        board = self.unmirrored_board()
        board.file("[Revert] Undo NAT-1: Header", body("Header"), 30, "Todo")

        self.assertIn("NAT-1: LIN-1 is its open revert ticket",
                      self.refused("NAT-1", "broke the header"))

    def test_a_merge_pushed_from_another_clone_is_fetched_as_a_dependent(self):
        self.project_with(ENABLED, {"src/header.py": "broken\n"})
        other = self.root / "other"
        subprocess.run(["git", "clone", "-q", "-b", "main",
                        str(self.root / "origin.git"), str(other)], check=True)
        (other / "src/header.py").write_text("upstream\n")
        subprocess.run(["git", "-c", "user.name=Tester", "-c",
                        "user.email=t@example.org", "commit", "-qam",
                        "NAT-2: upstream"], cwd=other, check=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=other,
                       check=True)
        upstream = subprocess.run(["git", "rev-parse", "HEAD"], cwd=other,
                                  capture_output=True, text=True,
                                  check=True).stdout.strip()
        with contextlib.closing(sqlite3.connect(self.project.store_path)) as conn:
            conn.execute("UPDATE runs SET mergeSha = ? WHERE id = ?",
                         (upstream, self.runs["NAT-2"]))
            conn.commit()

        code, out, err = self.holo("revert", "NAT-1", "broke the header",
                                   "--dry-run")

        self.assertEqual(code, 0, err)
        self.assertIn(f"dependent: NAT-2 {upstream} src/header.py", out)

    def test_a_git_failure_listing_the_paths_is_refused_naming_git(self):
        self.project_with(ENABLED, {"db/migrations/0002_add.py": "op\n"})
        self.git("config", "diff.orderFile", str(self.root / "missing"))

        self.assertIn("git diff failed", self.refused("NAT-1", "broke it"))

    def test_a_template_path_is_filed_in_a_form_the_checker_accepts(self):
        path = "template/{{cookiecutter.project_slug}}/app.py"
        self.project_with(ENABLED, {path: "broken\n"})

        code, out, err = self.holo("revert", "NAT-1", "broke the header")

        self.assertEqual(code, 0, out + err)
        self.assertIn("revert ticket NAT-3 filed", out)

    def test_a_ticket_quoting_the_reverts_line_is_no_open_revert(self):
        self.project_with(ENABLED, {"src/header.py": "broken\n"})
        quoting = self.root / "Quote.md"
        quoting.write_text(body("Audit").replace(
            "## Summary\n\n", f"## Summary\n\nIt quotes `Reverts: NAT-1 at"
                               f" {self.key_sha}`.\n\n", 1))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(holophyte.cli.entry.cli(
                [str(self.target), "--file-ticket", str(quoting)]), 0)

        code, out, err = self.holo("revert", "NAT-1", "broke the header")

        self.assertEqual(code, 0, err)
        self.assertIn("revert ticket NAT-4 filed", out)

    def test_startup_refuses_refuse_paths_that_are_not_a_list_of_strings(self):
        for value in ('"**/migrations/**"', "[1]"):
            with self.subTest(value=value):
                self.locate(f"{ENABLED}refuse_paths = {value}\n")
                with self.assertRaises(SystemExit) as raised, \
                        contextlib.redirect_stdout(io.StringIO()):
                    holophyte.cli.entry.cli([str(self.target)])
                self.assertIn("[revert] refuse_paths must be a list",
                              str(raised.exception.code))

    def test_startup_refuses_an_empty_negated_class_in_refuse_paths(self):
        for glob in ("db/changes/[!].sql", "db/changes/[^].sql"):
            with self.subTest(glob=glob):
                self.locate(f'{ENABLED}refuse_paths = ["{glob}"]\n')
                with self.assertRaises(SystemExit) as raised, \
                        contextlib.redirect_stdout(io.StringIO()):
                    holophyte.cli.entry.cli([str(self.target)])
                self.assertIn("is not a path glob",
                              str(raised.exception.code))


class RefusePathsMatchTimeTests(ConfigTestCase):
    def test_a_many_star_glob_against_a_long_near_miss_settles_promptly(self):
        probe = ("from holophyte.revert.checks import refused_paths\n"
                 "path = 'a' * 48 + '.ts'\n"
                 "print(refused_paths([path], ['*a' * 24 + 'b']),"
                 " refused_paths([path], ['*a' * 24 + '.ts']) == [path])\n")
        done = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                              text=True, timeout=20,
                              cwd=Path(__file__).resolve().parent.parent)
        self.assertEqual(done.stdout.split(), ["[]", "True"], done.stderr)
