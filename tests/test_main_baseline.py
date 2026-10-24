"""Each criterion with an on-main output clause has its check run by the
factory at the review base, with the candidate's copy of the named test file,
and the reviewer reads that run in a "Baseline at the merge base" block.

Real git in temporary repositories, real verify commands; agents are scripted.
Run: python3 -m unittest discover -s tests -p 'test_main_baseline.py' -v
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after sys.path
    IMPLEMENT,
    Commit,
    Idle,
    Reply,
    _git,
)
from loop_fixture import LoopFixture, StubProvider, a_task  # noqa: E402 - as above

import holophyte.config.project  # noqa: E402 - as above
import store  # noqa: E402 - as above
import store.tickets  # noqa: E402 - as above
from holophyte.review import main_baseline  # noqa: E402 - as above

VERIFY = "python3 -m unittest discover -s tests -p 'test_app.py'"
OLD, NEW = 'VALUE = "old"\n', 'VALUE = "new"\n'
TEST_APP = """import unittest

from app import VALUE


class AppTests(unittest.TestCase):
    def test_value(self):
        self.assertEqual(VALUE, {expected!r})
"""
OUTPUT = "and that run's output is in the implementer output."
ON_MAIN = ("Given `tests/test_app.py`, when it runs, then it passes. "
           f"On main the test fails, {OUTPUT}")
HEADING = "Baseline at the merge base"
APPROVE_APP = Reply("CRITERION 1: met — tests/test_app.py::AppTests::"
                    "test_value\nVERDICT: APPROVE")
REQUEST_CHANGES = Reply("Blocker: tidy the notes.\nVERDICT: REQUEST_CHANGES")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def payloads(conn, run):
    return [json.loads(payload) for (payload,) in conn.execute(
        "SELECT payload FROM runEvents WHERE runId = ? AND kind = ?"
        " ORDER BY seq", (run, main_baseline.EVENT)).fetchall()]


class BaselineRuns(unittest.TestCase):
    """`baselines()` over a real repository, its task worktree and a store."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Base", "GIT_AUTHOR_EMAIL": "base@example.com",
            "GIT_COMMITTER_NAME": "Base",
            "GIT_COMMITTER_EMAIL": "base@example.com",
            "PYTHONDONTWRITEBYTECODE": "1"}))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "app.py").write_text(OLD)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "main")
        self.project = holophyte.config.project.Project(
            path=self.repo, holo_dir=self.root,
            store_path=self.root / "store.db",
            config_path=self.root / "config.toml",
            worktrees=self.root / "wts")
        self.wt = self.root / "wts" / "ko-1"
        git(self.repo, "worktree", "add", "-q", "-b", "task/ko-1",
            str(self.wt), "main")
        self.commit({"app.py": NEW,
                     "tests/test_app.py": TEST_APP.format(expected="new")})

    def commit(self, files):
        for name, text in files.items():
            (self.wt / name).parent.mkdir(parents=True, exist_ok=True)
            (self.wt / name).write_text(text)
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", "candidate")
        return git(self.wt, "rev-parse", "HEAD")

    def run_store(self, criteria):
        handle, path = tempfile.mkstemp(suffix=".db", dir=self.root)
        os.close(handle)
        conn = store.open(path)
        self.addCleanup(conn.close)
        store.init(conn)
        project = store.tickets.ensure_project(conn, "team", str(self.repo))
        ticket = store.tickets.mirror_ticket(
            conn, project, "iss_1", linear_identifier="KO-1", title="t",
            acceptance_criteria=list(criteria),
            verification_commands=[VERIFY])
        return conn, store.claim(conn, project, ticket)

    def baselines(self, criteria, verify=VERIFY, at=None, session=None):
        conn, run = session or self.run_store(criteria)
        sha = at or git(self.wt, "rev-parse", "HEAD")
        results = main_baseline.baselines(self.project, conn, run, 1, self.wt,
                                          self.base, sha, criteria, verify,
                                          True)
        return conn, run, results

    def test_the_on_main_run_is_recorded_with_its_failing_output(self):
        conn, run, _ = self.baselines([ON_MAIN])
        (event,) = payloads(conn, run)
        self.assertEqual(event["criterion"], 1)
        self.assertEqual(event["base"], self.base)
        self.assertEqual(event["files"], ["tests/test_app.py"])
        self.assertEqual(event["command"], VERIFY)
        self.assertNotEqual(event["exit_status"], 0)
        self.assertIn("AssertionError", event["output"])

    def test_the_task_worktree_and_worktree_list_are_left_as_they_were(self):
        head = git(self.wt, "rev-parse", "HEAD")
        status = git(self.wt, "status", "--porcelain")
        trees = git(self.repo, "worktree", "list")
        self.baselines([ON_MAIN])
        self.assertEqual(git(self.wt, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.wt, "status", "--porcelain"), status)
        self.assertEqual(git(self.repo, "worktree", "list"), trees)

    def test_only_the_three_on_main_forms_naming_a_test_file_are_run(self):
        named = "Given `tests/test_app.py`, when it runs, then it passes. "
        criteria = [
            named + f"On main the test fails, {OUTPUT}",
            named + f"With `VALUE` as on main the test fails, {OUTPUT}",
            named + f"With the change reverted the test fails, {OUTPUT}",
            named + f"On the first ticket's branch the test fails, {OUTPUT}",
            named + "On main the test fails.",
            f"Given the app, when it runs, then it passes. On main the app "
            f"fails, {OUTPUT}",
        ]
        conn, run, _ = self.baselines(criteria)
        self.assertEqual([event["criterion"] for event in payloads(conn, run)],
                         [1, 2, 3])

    def stubs(self, *lines):
        """A verify block whose stubs log their own names; returns the block
        and the log they append to."""
        log = self.root / f"log-{len(list(self.root.glob('log-*')))}"
        block = []
        for name, args in lines:
            stub = self.root / f"{name}.sh"
            stub.write_text(f"echo {name} >> '{log}'\n")
            block.append(f"sh '{stub}' {args}".rstrip())
        return "\n".join(block), log

    def ran(self, criterion, *lines):
        block, log = self.stubs(*lines)
        self.baselines([criterion], verify=block)
        return log.read_text().split()

    def test_the_block_runs_up_to_the_first_line_naming_the_file(self):
        self.commit({"e2e/app.spec.ts": "test('app', () => {});\n"})
        spec = f"Given `e2e/app.spec.ts`, it passes. On main it fails, {OUTPUT}"
        for criterion, naming in ((ON_MAIN, "tests/test_app.py"),
                                  (ON_MAIN, "tests.test_app"),
                                  (spec, "app.spec")):
            with self.subTest(naming=naming):
                self.assertEqual(
                    self.ran(criterion, ("setup", ""), ("check", naming),
                             ("later", "")),
                    ["setup", "check"])

    def test_a_block_naming_no_file_runs_whole(self):
        self.assertEqual(
            self.ran(ON_MAIN, ("setup", ""), ("check", "--all"),
                     ("later", "")),
            ["setup", "check", "later"])

    def test_a_recorded_run_is_reused_until_the_named_file_changes(self):
        counter = self.root / "counter"
        verify = (f"python3 -c 'import uuid; print(uuid.uuid4())' | tee -a "
                  f"'{counter}' && {VERIFY}")
        session = self.run_store([ON_MAIN])
        _, _, (first,) = self.baselines([ON_MAIN], verify, session=session)
        _, _, again = self.baselines([ON_MAIN], verify, session=session)
        (token,) = counter.read_text().split()
        self.assertIn(token, first["output"])
        self.assertIn(token, main_baseline.brief(again))
        self.commit({"tests/test_app.py": TEST_APP.format(expected="new")
                     + "# revised\n"})
        self.baselines([ON_MAIN], verify, session=session)
        self.assertEqual(len(counter.read_text().split()), 2)


class Writes(Commit):
    """An implementer turn that writes several files in one commit."""

    role = IMPLEMENT

    def __init__(self, files):
        super().__init__("change and test")
        self.files = files

    def play(self, cwd, turn):
        for name, text in self.files.items():
            (cwd / name).parent.mkdir(parents=True, exist_ok=True)
            (cwd / name).write_text(text)
        _git(cwd, "add", "-A")
        _git(cwd, "commit", "-q", "-m", self.message)
        return f"committed: {self.message}"


CHANGE_AND_TEST = {"app.py": NEW,
                   "tests/test_app.py": TEST_APP.format(expected="new")}


class BaselineInTheLoop(LoopFixture):

    def setUp(self):
        super().setUp()
        (self.target / ".gitignore").write_text("__pycache__/\n")
        (self.target / "app.py").write_text(OLD)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "the app")
        self.base = self.git("rev-parse", "main").strip()

    def run_task(self, *script, criteria=(ON_MAIN,), verify=VERIFY):
        task = dict(a_task(), verify=verify, budget_min=15,
                    criteria=list(criteria))
        return self.loop(*script, provider=StubProvider(task))[0]

    def review_goals(self, fake):
        return [turn.goal for turn in fake.turns if turn.role == "review"]

    def recorded(self):
        return [json.loads(payload) for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'main_baseline'")]

    def test_both_review_goals_hold_the_baseline_block(self):
        fake = self.run_task(Writes(CHANGE_AND_TEST), REQUEST_CHANGES,
                             Commit("notes", path="NOTES.md"), APPROVE_APP)
        (event,) = self.recorded()
        goals = self.review_goals(fake)
        self.assertEqual(len(goals), 2)
        for goal in goals:
            block = goal[goal.index(HEADING):]
            self.assertIn("C1:", block)
            self.assertIn(f"merge base {self.base[:12]}", block)
            self.assertIn("with tests/test_app.py taken from the candidate",
                          block)
            self.assertIn(VERIFY, block)
            self.assertRegex(block, r"Exit status: [1-9]")
            self.assertIn(event["output"], block)
            self.assertIn("AssertionError", block)
            self.assertIn("This run is the run C1's on-main clause asks for, "
                          "in place of the implementer output", block)

    def test_a_ticket_without_an_on_main_clause_has_no_block(self):
        fake = self.run_task(
            Writes(CHANGE_AND_TEST), REQUEST_CHANGES,
            Commit("notes", path="NOTES.md"), APPROVE_APP,
            criteria=["A test in `tests/test_app.py` witnesses it."])
        goals = self.review_goals(fake)
        self.assertEqual(len(goals), 2)
        for goal in goals:
            self.assertNotIn(HEADING, goal)

    def test_a_check_passing_at_the_base_is_a_witness_turn_gap(self):
        fake = self.run_task(
            Writes({"tests/test_app.py": TEST_APP.format(expected="old")}),
            Idle(), APPROVE_APP)
        gap = (f"criterion C1's check passes at the merge base "
               f"{self.base[:12]}; its on-main clause says it fails")
        self.assertEqual(fake.roles[:2], ["implement", "implement"])
        self.assertIn(gap, fake.turns[1].goal)
        (goal,) = self.review_goals(fake)
        self.assertIn("Exit status: 0.", goal[goal.index(HEADING):])

    def logging_verify(self):
        """A verify that logs the directory each run starts in."""
        log = self.target.parent / "dirs.log"
        stub = self.target.parent / "where.sh"
        stub.write_text(f"pwd >> '{log}'\n")
        return f"sh '{stub}' && {VERIFY}", log

    def assert_no_baseline_ran(self, log):
        self.assertTrue(log.read_text().strip())
        self.assertFalse([line for line in log.read_text().splitlines()
                          if re.search(r"/tree$", line)])

    def test_a_red_verify_runs_no_baseline_and_the_block_says_why(self):
        verify, log = self.logging_verify()
        red = {"app.py": NEW,
               "tests/test_app.py": TEST_APP.format(expected="newer")}
        fake = self.run_task(Writes(red), REQUEST_CHANGES, Idle(),
                             verify=verify)
        goal = self.review_goals(fake)[0]
        self.assertIn(f"{HEADING}:\nC1: the baseline at {self.base[:12]} did "
                      "not run: verify failed", goal)
        self.assertEqual(self.recorded(), [])
        self.assert_no_baseline_ran(log)

    def test_a_failed_detached_setup_runs_no_baseline_and_the_block_says_why(self):
        self.configure("[worktree]\nsetup = ['case \"$PWD\" in */tree) echo "
                       "no-deps >&2; exit 3;; esac']\n")
        verify, log = self.logging_verify()
        fake = self.run_task(Writes(CHANGE_AND_TEST), APPROVE_APP,
                             verify=verify)
        (goal,) = self.review_goals(fake)
        block = goal[goal.index(HEADING):]
        self.assertIn("did not run: its worktree setup failed", block)
        self.assertIn("no-deps", block)
        self.assert_no_baseline_ran(log)


if __name__ == "__main__":
    unittest.main()
