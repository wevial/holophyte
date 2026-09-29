"""A story's witnesses run at one commit of main in a scratch worktree."""
import hashlib
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import store
from holophyte import config_tables
from holophyte.project import Project
from holophyte.witness import LOG_TAIL_BYTES, main_tip, red_kind, run_witnesses
from store.stories import approve_story, file_story, witness_ledger

PYTHON = shlex.quote(sys.executable)
FAILS_AN_ASSERTION = """import unittest


class W1Tests(unittest.TestCase):
    def test_sorted(self):
        self.assertEqual(sorted([2, 1]), [2, 1])
"""
IMPORTS_A_MISSING_MODULE = """import unittest

import orders_export_that_does_not_exist


class W2Tests(unittest.TestCase):
    def test_export(self):
        orders_export_that_does_not_exist.export()
"""
PASSES = """import unittest


class W3Tests(unittest.TestCase):
    def test_sorted(self):
        self.assertEqual(sorted([2, 1]), [1, 2])
"""
TWO_FAILURES = """import unittest


class T(unittest.TestCase):
    def test_a(self):
        self.assertEqual(1, 2)

    def test_b(self):
        self.assertTrue(False)
"""
A_FAILURE_AND_AN_ERROR = """import unittest


class T(unittest.TestCase):
    def test_a(self):
        self.assertEqual(1, 2)

    def test_b(self):
        {}["missing"]
"""


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def commit_file(repo, path, text, message):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def unittest_output(tmp, source):
    (Path(tmp) / "test_case.py").write_text(source)
    run = subprocess.run([sys.executable, "-m", "unittest", "test_case"],
                         cwd=tmp, capture_output=True, text=True)
    return run.stdout + run.stderr


class WitnessRunnerFixture:
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.root / "home")})
        home.start()
        self.addCleanup(home.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        commit_file(self.repo, "README.md", "orders\n", "initial")
        self.conn = store.open(self.root / "store.db")
        self.addCleanup(self.conn.close)
        self.marker = self.root / "w1-ran"

    def project(self, config=""):
        target = Project.locate(self.repo)
        target.config_path.parent.mkdir(parents=True, exist_ok=True)
        target.config_path.write_text(config)
        return target

    def approve(self, witnesses):
        self.conn.execute("INSERT INTO projects (linearTeamId, repoPath,"
                          " defaultBranch, autonomyProfile)"
                          " VALUES ('t', ?, 'main', 'personal')",
                          (str(self.repo),))
        ids = [self.conn.execute(
            "INSERT INTO tickets (projectId, linearIssueId, linearIdentifier,"
            " title, mirroredAt, status, affinity, revision, boardColumn,"
            " boardState, dependsOn) VALUES (1, ?, ?, 't', 1, 'ready', 'any',"
            " 1, 'ready', 'Ready', '[]')", (name, name)).lastrowid
            for name in ("KO-1", "KO-2")]
        self.conn.commit()
        file_story(self.conn, ids[0], [
            {"key": key, "criterion": f"{key} holds", "file": path,
             "command": command, "source": source}
            for key, path, command, source in witnesses],
            [(ids[1], "completes", [key for key, *_ in witnesses])])
        approve_story(self.conn, ids[0], 1, "operator", "go")
        self.story_id = ids[0]

    def three_witnesses(self):
        return [
            ("W1", "tests/test_w1.py",
             f"touch {shlex.quote(str(self.marker))} &&"
             f" {PYTHON} -m unittest tests.test_w1", FAILS_AN_ASSERTION),
            ("W2", "tests/test_w2.py", f"{PYTHON} -m unittest tests.test_w2",
             IMPORTS_A_MISSING_MODULE),
            ("W3", "tests/test_w3.py", f"{PYTHON} -m unittest tests.test_w3",
             PASSES),
        ]


class WitnessRunTests(WitnessRunnerFixture, unittest.TestCase):
    def test_copied_witnesses_are_judged_at_main_and_leave_no_worktree(self):
        target = self.project()
        witnesses = self.three_witnesses()
        self.approve(witnesses)
        tip = main_tip(target)

        rows = run_witnesses(target, self.conn, self.story_id, tip, "baseline",
                             copy_files=True)

        ledger = witness_ledger(self.conn, self.story_id, tip)
        self.assertEqual(rows, ledger)
        self.assertEqual(
            [(row.witnessKey, row.verdict, row.redKind, row.verifier)
             for row in ledger],
            [("W1", "red", "assert", "baseline"),
             ("W2", "red", "exception", "baseline"),
             ("W3", "green", None, "baseline")])
        for row, (_, _, _, source) in zip(ledger, witnesses):
            with self.subTest(witness=row.witnessKey):
                self.assertEqual(row.fileHash,
                                 hashlib.sha256(source.encode()).hexdigest())
                self.assertTrue(Path(row.evidencePath).is_file())
        self.assertIn("orders_export_that_does_not_exist",
                      Path(ledger[1].evidencePath).read_text())
        self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), 1)

    def test_a_witness_file_absent_at_the_commit_is_not_run(self):
        target = self.project()
        witnesses = self.three_witnesses()
        self.approve(witnesses)
        sha = commit_file(self.repo, "tests/test_w3.py", PASSES, "w3 lands")

        run_witnesses(target, self.conn, self.story_id, sha, "loop")

        self.assertEqual(
            [(row.witnessKey, row.verdict, row.fileHash is None)
             for row in witness_ledger(self.conn, self.story_id, sha)],
            [("W1", "absent", True), ("W2", "absent", True),
             ("W3", "green", False)])
        self.assertFalse(self.marker.exists())

    def test_a_witness_past_the_budget_is_an_error_and_is_stopped(self):
        target = self.project("[story]\nwitness_sec = 1\n")
        self.approve([("W1", "tests/test_w1.py", "sleep 5", PASSES)])
        tip = main_tip(target)

        started = time.monotonic()
        rows = run_witnesses(target, self.conn, self.story_id, tip, "loop",
                             copy_files=True)

        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual([(row.verdict, row.redKind) for row in rows],
                         [("error", None)])


class WitnessEvidenceTests(WitnessRunnerFixture, unittest.TestCase):
    def test_a_later_run_at_the_same_commit_keeps_the_earlier_evidence(self):
        target = self.project()
        flag = self.root / "flag"
        self.approve([("W1", "tests/test_w1.py",
                       f"cat {shlex.quote(str(flag))}", PASSES)])
        tip = main_tip(target)

        (red,) = run_witnesses(target, self.conn, self.story_id, tip,
                               "baseline", copy_files=True)
        red_evidence = Path(red.evidencePath).read_text()
        flag.write_text("the flag is up\n")
        (green,) = run_witnesses(target, self.conn, self.story_id, tip,
                                 "baseline", copy_files=True)

        self.assertEqual((red.verdict, green.verdict), ("red", "green"))
        self.assertNotEqual(red.evidencePath, green.evidencePath)
        self.assertEqual(Path(red.evidencePath).read_text(), red_evidence)
        self.assertEqual(Path(green.evidencePath).read_text(),
                         "the flag is up\n")

    def test_every_approved_file_is_copied_before_the_first_command(self):
        target = self.project()
        self.approve([
            ("W1", "tests/test_w1.py", f"{PYTHON} -m unittest tests.test_w1",
             "import unittest\n\nfrom tests.test_w2 import W3Tests  # noqa\n"),
            ("W2", "tests/test_w2.py", f"{PYTHON} -m unittest tests.test_w2",
             PASSES)])

        rows = run_witnesses(target, self.conn, self.story_id,
                             main_tip(target), "baseline", copy_files=True)

        self.assertEqual([(row.witnessKey, row.verdict) for row in rows],
                         [("W1", "green"), ("W2", "green")])


class WitnessIsolationTests(WitnessRunnerFixture, unittest.TestCase):
    def test_a_command_cannot_change_a_later_witness_verdict(self):
        target = self.project()
        self.approve([
            ("W1", "tests/test_w1.py",
             "rm tests/test_w2.py && cp tests/test_w1.py tests/test_w3.py",
             PASSES),
            ("W2", "tests/test_w2.py", f"{PYTHON} -m unittest tests.test_w2",
             PASSES),
            ("W3", "tests/test_w3.py", "true", PASSES)])
        commit_file(self.repo, "tests/test_w1.py", PASSES, "w1 lands")
        sha = commit_file(self.repo, "tests/test_w2.py", PASSES, "w2 lands")

        rows = run_witnesses(target, self.conn, self.story_id, sha, "loop")

        self.assertEqual([(row.witnessKey, row.verdict) for row in rows],
                         [("W1", "green"), ("W2", "green"), ("W3", "absent")])
        self.assertEqual(rows[1].fileHash,
                         hashlib.sha256(PASSES.encode()).hexdigest())

    def test_a_witness_path_outside_the_checkout_is_refused_unrun(self):
        target = self.project()
        escape = self.root / "escape.py"
        outside = "../../" + os.path.relpath(escape, tempfile.gettempdir())
        absolute = self.root / "absolute.py"
        touch = f"touch {shlex.quote(str(self.marker))}"
        self.approve([("W1", outside, touch, PASSES),
                      ("W2", str(absolute), touch, PASSES),
                      ("W3", "tests/test_w3.py",
                       f"{PYTHON} -m unittest tests.test_w3", PASSES)])

        rows = run_witnesses(target, self.conn, self.story_id,
                             main_tip(target), "baseline", copy_files=True)

        self.assertEqual([(row.witnessKey, row.verdict) for row in rows],
                         [("W1", "error"), ("W2", "error"), ("W3", "green")])
        self.assertFalse(escape.exists())
        self.assertFalse(absolute.exists())
        self.assertFalse(self.marker.exists())


class WitnessOutputTests(WitnessRunnerFixture, unittest.TestCase):
    def test_a_command_that_runs_no_tests_is_an_error(self):
        target = self.project()
        self.approve([("W1", "tests/test_w1.py",
                       f"{PYTHON} -m unittest discover -s tests"
                       " -p 'test_nothing*.py' 2>&1 | tail -3", PASSES)])

        (row,) = run_witnesses(target, self.conn, self.story_id,
                               main_tip(target), "baseline", copy_files=True)

        self.assertEqual(row.verdict, "error")
        self.assertIn("Ran 0 tests", Path(row.evidencePath).read_text())

    def test_a_stored_log_keeps_only_its_tail(self):
        target = self.project()
        self.approve([("W1", "tests/test_w1.py",
                       f"{PYTHON} -c \"print('x' * 300000); print('the end')\"",
                       PASSES)])

        (row,) = run_witnesses(target, self.conn, self.story_id,
                               main_tip(target), "baseline", copy_files=True)

        stored = Path(row.evidencePath).read_bytes()
        self.assertEqual(row.verdict, "green")
        self.assertLessEqual(len(stored), LOG_TAIL_BYTES + 100)
        self.assertTrue(stored.endswith(b"x\nthe end\n"))

    def test_a_known_secret_is_redacted_from_the_stored_log(self):
        secret = "ghp_witnessLogSecret0123456789"
        token = patch.dict(os.environ, {"GH_TOKEN": secret})
        token.start()
        self.addCleanup(token.stop)
        target = self.project()
        self.approve([("W1", "tests/test_w1.py",
                       f"echo token {secret} printed", PASSES)])

        (row,) = run_witnesses(target, self.conn, self.story_id,
                               main_tip(target), "baseline", copy_files=True)

        stored = Path(row.evidencePath).read_text()
        self.assertNotIn(secret, stored)
        self.assertIn("token [redacted] printed", stored)


class WitnessWorktreeTests(WitnessRunnerFixture, unittest.TestCase):
    def post_checkout(self, body):
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\n{body}\n")
        hook.chmod(0o755)

    def test_a_worktree_add_past_the_budget_is_an_error_and_is_stopped(self):
        target = self.project("[story]\nwitness_sec = 1\n")
        self.approve([("W1", "tests/test_w1.py", "true", PASSES)])
        tip = main_tip(target)
        self.post_checkout("sleep 5")

        started = time.monotonic()
        rows = run_witnesses(target, self.conn, self.story_id, tip, "loop",
                             copy_files=True)

        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual([row.verdict for row in rows], ["error"])
        self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), 1)

    def test_a_failed_worktree_add_is_an_error_and_leaves_no_worktree(self):
        target = self.project()
        self.approve([("W1", "tests/test_w1.py", "true", PASSES)])
        tip = main_tip(target)
        self.post_checkout("exit 1")

        rows = run_witnesses(target, self.conn, self.story_id, tip, "loop",
                             copy_files=True)

        self.assertEqual([row.verdict for row in rows], ["error"])
        self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), 1)


class WitnessBudgetConfigTests(WitnessRunnerFixture, unittest.TestCase):
    def test_the_budget_defaults_to_ten_minutes(self):
        self.assertEqual(
            config_tables.story_config(self.project()).witness_sec, 600)

    def test_a_budget_that_is_not_a_positive_integer_is_refused(self):
        for value in ("0", '"600"', "true", "1.5"):
            with self.subTest(value=value), \
                    self.assertRaisesRegex(SystemExit, r"\[story\] witness_sec"):
                config_tables.story_config(
                    self.project(f"[story]\nwitness_sec = {value}\n"))


class RedKindTests(unittest.TestCase):
    def test_a_red_is_an_assert_only_when_only_assertions_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            two_failures = unittest_output(tmp, TWO_FAILURES)
            failure_and_error = unittest_output(tmp, A_FAILURE_AND_AN_ERROR)
        self.assertIn("FAILED (failures=2)", two_failures)
        self.assertIn("FAILED (failures=1, errors=1)", failure_and_error)
        pytest_asserts = (
            "=========================== short test summary info"
            " ============================\n"
            "FAILED tests/test_w1.py::test_sorted - AssertionError: assert"
            " [1, 2] == [2, 1]\n"
            "FAILED tests/test_w1.py::test_total - AssertionError: assert 3"
            " == 4\n"
            "============================== 2 failed in 0.02s"
            " ===============================\n")
        pytest_attribute = (
            "=========================== short test summary info"
            " ============================\n"
            "FAILED tests/test_w1.py::test_sorted - AssertionError: assert"
            " [1, 2] == [2, 1]\n"
            "FAILED tests/test_w1.py::test_export - AttributeError: module"
            " 'orders' has no attribute 'export'\n"
            "============================== 2 failed in 0.02s"
            " ===============================\n")
        pytest_plain_assert = (
            "FAILED tests/test_w1.py::test_value - assert 1 == 2\n"
            "FAILED tests/test_w1.py::test_total - AssertionError: 3 != 4\n"
            "============================== 2 failed in 0.02s"
            " ===============================\n")
        pytest_assert_and_error = (
            "FAILED tests/test_w1.py::test_value - assert 1 == 2\n"
            "ERROR tests/test_w2.py - ModuleNotFoundError: No module named"
            " 'orders'\n")
        spaced_assert = (
            "FAILED tests/test_values.py::test_value[hello world] -"
            " AssertionError: assert 'hello' == 'world'\n")
        spaced_error_and_assert = (
            "FAILED tests/test_values.py::test_value - assert 1 == 2\n"
            "ERROR tests/test_values.py::test_value[hello world] -"
            " RuntimeError: setup failed\n")
        cases = [(two_failures, "assert"), (failure_and_error, "exception"),
                 (pytest_asserts, "assert"), (pytest_attribute, "exception"),
                 (pytest_plain_assert, "assert"),
                 (pytest_assert_and_error, "exception"),
                 (spaced_assert, "assert"),
                 (spaced_error_and_assert, "exception"),
                 ("Segmentation fault (core dumped)\n", "exception")]
        for output, kind in cases:
            with self.subTest(kind=kind, output=output[-60:]):
                self.assertEqual(red_kind(output), kind)


class MainTipTests(WitnessRunnerFixture, unittest.TestCase):
    def test_a_local_project_answers_main(self):
        self.assertEqual(main_tip(self.project()),
                         git(self.repo, "rev-parse", "main"))

    def test_a_pull_request_project_fetches_and_answers_origin_main(self):
        bare = self.root / "origin.git"
        git(self.root, "clone", "-q", "--bare", str(self.repo), str(bare))
        git(self.repo, "remote", "add", "origin", str(bare))
        other = self.root / "other"
        git(self.root, "clone", "-q", str(bare), str(other))
        remote = commit_file(other, "CHANGES.md", "more\n", "remote only")
        git(other, "push", "-q", "origin", "main")

        tip = main_tip(self.project('[merge]\nmode = "pr"\n'))

        self.assertEqual(tip, remote)
        self.assertNotEqual(git(self.repo, "rev-parse", "main"), remote)

    def test_a_fetch_that_does_not_answer_is_stopped_and_reported(self):
        bare = self.root / "origin.git"
        git(self.root, "clone", "-q", "--bare", str(self.repo), str(bare))
        git(self.repo, "remote", "add", "origin", str(bare))
        git(self.repo, "config", "remote.origin.uploadpack",
            "sleep 5; git-upload-pack")
        target = self.project('[merge]\nmode = "pr"\n')

        started = time.monotonic()
        with patch("holophyte.pr.PR_TIMEOUT", 1), \
                self.assertRaisesRegex(RuntimeError, "did not answer in 1s"):
            main_tip(target)

        self.assertLess(time.monotonic() - started, 4)


if __name__ == "__main__":
    unittest.main()
