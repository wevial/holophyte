"""`[merge] private_patterns`: what the factory publishes is scanned, and a
match is refused naming its location and pattern index, never its text."""
import contextlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402
from fake_agent import APPROVE, Commit, Idle  # noqa: E402
from loop_fixture import (  # noqa: E402
    LoopFixture,
    MergeModeFixture,
    StubProvider,
    a_task,
)

import holophyte.cli.entry  # noqa: E402
import holophyte.redact  # noqa: E402
import linear_provider  # noqa: E402
import ticket_template  # noqa: E402
from holophyte.config.checks import check_config  # noqa: E402
from holophyte.leak_guard import PrivateMatch  # noqa: E402
from holophyte.loop.gates import InfraFailure  # noqa: E402
from holophyte.pr import github, pr_status, pullrequest  # noqa: E402
from tests.test_provider import ticket_body  # noqa: E402

PATTERNS = r"['\bnever-published\.invalid\b', '(?i)\bbuild-host\.lan\b']"
CONFIG = f"[merge]\nprivate_patterns = {PATTERNS}\n"
LEAK = "http://Build-Host.lan/api"
CLEAN = "http://host.example/api"
KEY = "[merge] private_patterns #1"


def hold_redactions(case):
    """Matches a test registers stay out of the modules that run after it."""
    patcher = patch.object(holophyte.redact, "_environment_values",
                           holophyte.redact._environment_values)
    patcher.start()
    case.addCleanup(patcher.stop)


class PrivateText:
    def assertPrivateAbsent(self, text):
        self.assertNotIn("build-host", text.lower())
        self.assertNotIn(r"\bbuild", text)


class PatternConfigTests(PrivateText, ConfigTestCase):
    def refusal(self, value):
        self.locate(f"[merge]\nprivate_patterns = {value}\n")
        with self.assertRaises(SystemExit) as raised:
            check_config(self.project)
        return str(raised.exception)

    def test_an_invalid_pattern_or_a_non_list_refuses_startup_without_its_text(self):
        message = self.refusal(
            r"['\bok\b', '(?i)\bbuild-host\.lan', '(?i)build-host[']")
        self.assertIn("private_patterns #2", message)
        self.assertIn("not a valid regular expression", message)
        self.assertPrivateAbsent(message)

        message = self.refusal(r"['(?i)\bbuild-host\.lan', 3]")
        self.assertIn("private_patterns #1 is not a string", message)
        self.assertPrivateAbsent(message)

        message = self.refusal(r"'(?i)\bbuild-host\.lan\b'")
        self.assertIn("private_patterns must be a list", message)
        self.assertPrivateAbsent(message)


class RealGit(unittest.TestCase):
    def setUp(self):
        hold_redactions(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.remote = self.root / "remote.git"
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test Writer")
        self.git("config", "user.email", "writer@example.com")
        self.commit("base", "README.md", "base\n")
        self.git("init", "--bare", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "origin", "main")
        self.git("checkout", "-b", "task")
        self.config = {"merge": {"private_patterns": [
            r"\bnever-published\.invalid\b", r"(?i)\bbuild-host\.lan\b"]}}
        self.target = SimpleNamespace(path=self.repo, config=lambda: self.config,
                                      config_path=self.root / "config.toml")

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd or self.repo,
                                       stderr=subprocess.PIPE, text=True).strip()

    def commit(self, message, path="tests/test_api.py", text=None):
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            file.unlink()
        else:
            file.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def remote_task(self):
        return self.git("ls-remote", "origin", "refs/heads/task")


class PushTests(PrivateText, RealGit):
    def refused_push(self):
        tip = self.git("rev-parse", "task")
        with self.assertRaises(PrivateMatch) as raised:
            github.push_branch(self.target, "task")
        self.assertIsInstance(raised.exception, InfraFailure)
        self.assertEqual(self.remote_task(), "")
        self.assertEqual(self.git("rev-parse", "task"), tip)
        message = str(raised.exception)
        self.assertPrivateAbsent(message)
        return message

    def pushed(self):
        github.push_branch(self.target, "task")
        self.assertEqual(self.remote_task().split()[0],
                         self.git("rev-parse", "task"))

    def test_a_branch_adding_a_matching_line_is_not_pushed(self):
        self.commit("Add the client test", text=f"def test():\n    URL = '{LEAK}'\n")
        message = self.refused_push()
        self.assertIn(f"tests/test_api.py:2 ({KEY})", message)
        self.assertIn("nothing pushed", message)

    def test_a_matching_commit_message_is_refused_naming_the_commit(self):
        sha = self.commit(f"Add the client test\n\nRecorded against {LEAK}.",
                          text=f"URL = '{CLEAN}'\n")
        message = self.refused_push()
        short = self.git("rev-parse", "--short", sha)
        self.assertIn(f"commit {short} message line 3 ({KEY})", message)

    def test_deleting_a_published_match_and_a_clean_branch_both_push(self):
        self.git("checkout", "main")
        self.commit("Add the client test", text=f"URL = '{LEAK}'\n")
        self.git("push", "origin", "main")
        self.git("checkout", "-b", "removal")
        self.git("branch", "-D", "task")
        self.git("branch", "-m", "task")
        self.commit("Drop the client test")
        self.pushed()

        self.commit("Add a clean test", "tests/test_other.py", f"URL = '{CLEAN}'\n")
        self.pushed()

    def test_a_merge_of_main_does_not_rescan_main_lines(self):
        self.commit("Task work", "tests/test_other.py", f"URL = '{CLEAN}'\n")
        self.git("checkout", "main")
        self.commit("Main work", text=f"URL = '{LEAK}'\n")
        self.git("checkout", "task")
        self.git("merge", "--no-ff", "-m", "Merge main", "main")
        self.pushed()

    def test_with_no_patterns_a_matching_line_is_pushed(self):
        self.config = {}
        self.commit("Add the client test", text=f"URL = '{LEAK}'\n")
        self.pushed()


class PullRequestTextTests(PrivateText, RealGit):
    def setUp(self):
        super().setUp()
        bindir = self.root / "bin"
        bindir.mkdir()
        self.calls = self.root / "gh.log"
        gh = bindir / "gh"
        gh.write_text(f'#!/bin/sh\necho "$*" >> "{self.calls}"\ncat >/dev/null\n'
                      'echo https://github.com/example/repo/pull/7\n')
        gh.chmod(0o755)
        path = patch.dict(os.environ,
                          {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"})
        path.start()
        self.addCleanup(path.stop)
        self.pull = pr_status.parse_pr_url("https://github.com/example/repo/pull/7")

    def gh_calls(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_a_matching_body_is_refused_before_gh_is_called(self):
        body = f"Adds the client.\n\nTested against {LEAK}."
        for call in (lambda: github.create_pull_request(
                         self.target, "task", "KO-1: add the client", body),
                     lambda: github.edit_pr_body(self.target, self.pull, body)):
            with self.assertRaises(PrivateMatch) as raised:
                call()
            self.assertIn(f"pull request body line 3 ({KEY})",
                          str(raised.exception))
            self.assertPrivateAbsent(str(raised.exception))
        self.assertEqual(self.gh_calls(), [])

        github.edit_pr_body(self.target, self.pull, "Adds the client.")
        self.assertEqual(len(self.gh_calls()), 1)

    def test_push_and_open_refuses_matching_text_before_the_push(self):
        self.commit("Add the client test", text=f"URL = '{CLEAN}'\n")
        before = self.git("ls-remote", "origin")
        for title, text in ((f"KO-1: point at {LEAK}", "Adds the client."),
                            ("KO-1: add the client", f"Uses {LEAK}.")):
            with self.subTest(title=title), \
                    self.assertRaises(PrivateMatch) as raised:
                pullrequest._push_and_open(self.target, None, None, "task",
                                           title, text, 60)
            self.assertPrivateAbsent(str(raised.exception))
            self.assertEqual(self.git("ls-remote", "origin"), before)
        self.assertEqual(self.gh_calls(), [])


NATIVE = '[board]\nkind = "native"\nprefix = "NAT"\n'


def no_linear(*args, **kwargs):
    raise AssertionError("the command line asked Linear")


class TicketFilingTests(PrivateText, ConfigTestCase):
    def setUp(self):
        hold_redactions(self)
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.locate(NATIVE + CONFIG)
        (self.target / "tests").mkdir()
        (self.target / "tests" / "test_thing.py").write_text("")

    def ticket(self, name, notes):
        text = ticket_body(title="Point the client", verify=(
            "python3 -m unittest discover -s tests -p 'test_thing.py'")).replace(
            "- None worth noting.", notes)
        path = self.root / name
        path.write_text(text)
        return path, text.splitlines().index(notes) + 1

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = holophyte.cli.entry.cli([str(self.target), "--file-ticket",
                                              *map(str, args)])
        return status, out.getvalue()

    def rows(self):
        if not self.project.store_path.exists():
            return []
        with contextlib.closing(sqlite3.connect(self.project.store_path)) as conn:
            return conn.execute("SELECT linearIdentifier, body, revision"
                                " FROM tickets").fetchall()

    def test_check_file_and_update_refuse_a_matching_ticket(self):
        leaked, line = self.ticket("leak.md", f"- The API lives at {LEAK}.")
        named = f"section 'Implementation notes', line {line},"

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = ticket_template.main(["--repo", str(self.target), str(leaked)])
        self.assertEqual(status, 1)
        self.assertIn(named, out.getvalue())
        self.assertIn(KEY, out.getvalue())
        self.assertPrivateAbsent(out.getvalue())

        status, printed = self.cli(leaked)
        self.assertEqual(status, 1)
        self.assertIn(named, printed)
        self.assertIn(KEY, printed)
        self.assertPrivateAbsent(printed)
        self.assertEqual(self.rows(), [])

        clean, _ = self.ticket("clean.md", f"- The API lives at {CLEAN}.")
        self.assertEqual(self.cli(clean)[0], 0)
        filed = self.rows()
        status, printed = self.cli(leaked, "--update", "NAT-1", "--revision", "1")
        self.assertEqual(status, 1)
        self.assertIn(named, printed)
        self.assertIn(KEY, printed)
        self.assertPrivateAbsent(printed)
        self.assertEqual(self.rows(), filed)


class Amend(Commit):
    def play(self, cwd, turn):
        (cwd / self.path).write_text(self.body)
        subprocess.run(["git", "commit", "-qa", "--amend", "--no-edit"],
                       cwd=cwd, check=True)
        return "amended the commit that added the line"


class ReviewRoundTests(PrivateText, LoopFixture):
    def setUp(self):
        super().setUp()
        hold_redactions(self)

    def test_an_approved_candidate_adding_a_match_gets_a_fix_turn(self):
        self.configure(CONFIG)
        out = self.main_output(
            Commit("Add the client test", path="tests/test_api.py",
                   body=f"def test_api():\n    URL = '{LEAK}'\n"),
            APPROVE,
            Amend(path="tests/test_api.py",
                  body=f"def test_api():\n    URL = '{CLEAN}'\n"),
            APPROVE, provider=StubProvider(dict(a_task(), verify="echo ok")))

        fix_goal = self.last_fake.turns[2].goal
        self.assertIn(f"tests/test_api.py:2 holds text the project does not"
                      f" publish ({KEY})", fix_goal)
        ((verdict,),) = self.read(
            "SELECT verdict FROM reviewRounds WHERE round = 1")
        self.assertEqual(verdict, "changes_requested")
        ((summary,),) = self.read(
            "SELECT summary FROM runEvents WHERE kind = 'private_match'")
        self.assertIn(f"tests/test_api.py:2 ({KEY})", summary)
        for (text,) in self.read("SELECT summary FROM runEvents"):
            self.assertPrivateAbsent(text)
        self.assertPrivateAbsent(out)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])


class PullRequestRunTests(PrivateText, MergeModeFixture):
    def setUp(self):
        super().setUp()
        hold_redactions(self)

    def test_a_stub_body_that_matches_fails_the_run_before_the_push(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       f"private_patterns = {PATTERNS}\n")
        self.fake_route()
        body = self.BODY.replace("The thing, added.",
                                 f"The thing, served from {LEAK}.")
        out = io.StringIO()
        with patch.object(sys, "stdout", out):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=StubProvider(dict(a_task(), body=body)))

        self.assertFalse([c for c in self.recorded() if c.startswith("git push")])
        ((summary,),) = self.read(
            "SELECT summary FROM runEvents WHERE kind = 'private_match'")
        self.assertIn(f"pull request body line 1 ({KEY})", summary)
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn(f"pull request body line 1 ({KEY})", reason)
        for text in (reason, out.getvalue(), *(s for (s,) in self.read(
                "SELECT summary FROM runEvents"))):
            self.assertPrivateAbsent(text)


if __name__ == "__main__":
    unittest.main()
