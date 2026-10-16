"""The witness pre-check reads each criterion's named test file from git and a
verbose rerun of the verify's real unittest, before the first review.

Run: python3 -m unittest discover -s tests -p 'test_witness_precheck.py' -v
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit, Idle  # noqa: E402 - after sys.path
from loop_fixture import LoopFixture, StubProvider, a_task  # noqa: E402 - as above

from holophyte.review import witness_precheck  # noqa: E402 - as above

VERIFY = "python3 -m unittest discover -s tests -p 'test_*.py'"
RUNS = """import unittest


class Runs(unittest.TestCase):
    def test_it_runs(self):
        self.assertTrue(True)
"""
GATED = """import unittest


class Gated(unittest.TestCase):
    @unittest.skip("needs a browser")
    def test_it_draws(self):
        pass
"""
MIXED = """import unittest


class Mixed(unittest.TestCase):
    def test_it_counts(self):
        self.assertEqual(1 + 1, 2)

    @unittest.skip("needs a browser")
    def test_it_draws(self):
        pass
"""
MIXIN = """import unittest


class _Draws:
    def test_it_draws(self):
        self.assertTrue(True)


class Drawing(_Draws, unittest.TestCase):
    pass
"""
MODULE_SKIPPED = """import pytest

pytest.skip("needs a browser", allow_module_level=True)


def test_it_draws():
    pass
"""


class Repository(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "factory@example.invalid")
        self.git("config", "user.name", "Factory Test")
        self.commit({".gitignore": "__pycache__/\n", "README.md": "base\n"})
        self.base = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, files):
        for path, body in files.items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(body)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")
        return self.git("rev-parse", "HEAD")

    def check(self, criteria, verify=VERIFY):
        sha = self.git("rev-parse", "HEAD")
        return sha, witness_precheck.check(self.root, self.base, sha, criteria,
                                           verify, self.run_shell)

    def run_shell(self, command):
        return subprocess.run(command, shell=True, cwd=self.root, text=True,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT).stdout


class NamedFiles(unittest.TestCase):

    def test_each_criterion_names_its_test_files_in_order(self):
        criteria = [
            "A test in `tests/test_pr_media.py` witnesses it.",
            "Given that brief, then it has no video sentence (the same test"
            " file).",
            "`.venv/bin/python -m unittest tests.test_docs_links -v` passes.",
            "when `e2e/checks/images.spec.ts` runs, then both width checks"
            " pass",
            "`docs/config.md` describes it, and `tests/test_docs*.py` pass",
            "Given the page, then it shows load",
        ]

        self.assertEqual(witness_precheck.named_files(criteria), [
            ["tests/test_pr_media.py"], ["tests/test_pr_media.py"],
            ["tests/test_docs_links.py"], ["e2e/checks/images.spec.ts"],
            ["tests/test_docs*.py"], []])


class ExistsAndChanged(Repository):

    def test_a_named_file_must_exist_and_be_added_unless_read_as_existing(self):
        self.commit({"tests/test_old.py": RUNS})
        self.base = self.git("rev-parse", "HEAD")
        self.commit({"tests/test_new.py": RUNS})

        sha, entries = self.check([
            "A test in `tests/test_new.py` witnesses it.",
            "A test in `tests/test_old.py` witnesses it.",
            "`tests/test_old.py` passes unchanged.",
            "A test in `tests/test_gone.py` witnesses it.",
        ], verify=None)

        gaps = [entry["gaps"] for entry in entries]
        self.assertEqual(gaps[0], [])
        self.assertEqual(gaps[1], ["criterion 2 names `tests/test_old.py`; "
                                   "this branch neither adds nor changes it"])
        self.assertEqual(gaps[2], [])
        self.assertEqual(gaps[3], ["criterion 4 names `tests/test_gone.py`; "
                                   f"it does not exist at {sha[:12]}"])


class Ran(Repository):

    def test_a_file_whose_only_test_is_skipped_is_a_gap_with_its_reason(self):
        self.commit({"tests/test_runs.py": RUNS, "tests/test_gated.py": GATED})

        _, entries = self.check([
            "A test in `tests/test_runs.py` witnesses it.",
            "A test in `tests/test_gated.py` witnesses it.",
        ])

        self.assertEqual(entries[0]["gaps"], [])
        self.assertEqual(entries[0]["checks"][0]["ran"], "ran")
        self.assertEqual(entries[1]["gaps"], [
            "criterion 2 names `tests/test_gated.py`; its tests ran only "
            "skipped in verify (needs a browser)"])

    def test_an_added_test_skipped_beside_a_running_one_is_named(self):
        self.commit({"tests/test_mixed.py": MIXED})

        _, (entry,) = self.check(
            ["A test in `tests/test_mixed.py` witnesses it."])

        self.assertEqual(entry["checks"][0]["ran"], "added_skipped")
        self.assertEqual(entry["gaps"], [
            "criterion 1 names `tests/test_mixed.py`; "
            "`tests/test_mixed.py::Mixed::test_it_draws` was skipped in verify"
            " (needs a browser)"])

    def test_a_file_the_verify_never_collects_is_not_run(self):
        self.commit({"tests/test_runs.py": RUNS, "tests/test_other.py": RUNS})

        _, entries = self.check(
            ["A test in `tests/test_runs.py` witnesses it.",
             "A test in `tests/test_other.py` witnesses it."],
            verify="python3 -m unittest discover -s tests -p 'test_runs.py'")

        self.assertEqual(entries[0]["gaps"], [])
        self.assertEqual(entries[1]["gaps"], [
            "criterion 2 names `tests/test_other.py`; the verify commands run"
            " no test from it"])


class RanPerFile(Repository):

    def test_each_file_a_glob_matches_is_checked_on_its_own(self):
        self.commit({"tests/test_docs_runs.py": RUNS,
                     "tests/test_docs_gated.py": GATED})

        _, (entry,) = self.check(["`tests/test_docs_*.py` pass"])

        self.assertEqual(entry["gaps"], [
            "criterion 1 names `tests/test_docs_gated.py`; its tests ran only "
            "skipped in verify (needs a browser)"])

    def test_an_added_mixin_test_run_through_its_test_case_ran(self):
        self.commit({"tests/test_drawing.py": MIXIN})

        _, (entry,) = self.check(
            ["A test in `tests/test_drawing.py` witnesses it."])

        self.assertEqual(entry["checks"][0]["ran"], "ran")
        self.assertEqual(entry["gaps"], [])


class RanUnderPytest(Repository):

    @unittest.skipIf(subprocess.run(["python3", "-c", "import pytest"],
                                    capture_output=True).returncode,
                     "pytest is not installed for python3")
    def test_a_module_skipped_at_collection_keeps_its_reason(self):
        self.commit({"tests/test_runs.py": RUNS,
                     "tests/test_gated.py": MODULE_SKIPPED})

        _, (entry,) = self.check(
            ["A test in `tests/test_gated.py` witnesses it."],
            verify="python3 -m pytest -q tests")

        self.assertEqual(entry["gaps"], [
            "criterion 1 names `tests/test_gated.py`; its tests ran only "
            "skipped in verify (needs a browser)"])


class Unchecked(Repository):

    def test_a_spec_file_is_unchecked_and_an_unnamed_criterion_is_no_file(self):
        self.commit({"e2e/checks/x.spec.ts": "test('a', () => {});\n"})

        _, (spec, unnamed) = self.check([
            "when `e2e/checks/x.spec.ts` runs, then it passes",
            "Given the page, then it shows load"])

        self.assertEqual(spec["gaps"], [])
        self.assertEqual(spec["checks"][0]["ran"], "unchecked")
        self.assertEqual(unnamed["named"], "no_file")
        self.assertEqual(unnamed["gaps"], [])


class InTheLoop(LoopFixture):
    CRITERION = "A test in `tests/test_gated.py` witnesses it."

    def setUp(self):
        super().setUp()
        (self.target / ".gitignore").write_text("__pycache__/\n")
        (self.target / "tests" / "test_runs.py").write_text(RUNS)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "a running test")
        self.base = self.git("rev-parse", "main").strip()

    def run_task(self, verify, *after, turns=()):
        task = dict(a_task(), verify=verify, criteria=[self.CRITERION])
        return self.loop(Commit("gated test", path="tests/test_gated.py",
                                body=GATED), *turns, APPROVE, *after,
                         provider=StubProvider(task))[0]

    def events(self):
        return self.read("SELECT seq, kind, level, summary, payload"
                         " FROM runEvents ORDER BY seq")

    def test_round_one_records_the_skipped_gap_before_the_review(self):
        fake = self.run_task(VERIFY, turns=[Idle()])

        events = self.events()
        prechecks = [event for event in events
                     if event[1] == "witness_precheck"]
        results = [event for event in events
                   if event[1] == "witness_precheck_results"]
        modes = [event for event in events if event[1] == "review_mode"]
        self.assertEqual(len(prechecks), 1)
        ((seq, _, level, text, _),) = prechecks
        self.assertEqual(level, "narrative")
        self.assertLess(seq, modes[0][0])
        self.assertIn("criterion 1 names `tests/test_gated.py`; its tests ran"
                      " only skipped in verify (needs a browser)", text)
        ((_, _, level, _, payload),) = results
        self.assertEqual(level, "detail")
        self.assertEqual([entry["criterion"] for entry in
                          json.loads(payload)["criteria"]], [1])
        self.assertEqual(json.loads(modes[0][4])["round"], 1)
        self.assertEqual(fake.roles[:3], ["implement", "implement", "review"])

    def test_a_failing_verify_records_that_the_check_did_not_run(self):
        self.run_task("false", Idle())

        prechecks = [event[3] for event in self.events()
                     if event[1] == "witness_precheck"]
        self.assertEqual(prechecks,
                         ["witness pre-check not run: verify failed"])


if __name__ == "__main__":
    unittest.main()
