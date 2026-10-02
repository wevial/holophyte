"""The evidence check counts the candidate's new tests before judging them.

A bug ticket's candidate whose new tests all skip where the check runs goes
to a fix round asking for a runnable reproduction; one whose tests run is
judged as before. Verify is real unittest in a temporary repository whose
base ignores `__pycache__/`, as a real one does, and already has a running
test whose name contains the new tests' name.
Run: python3 -m unittest discover -s tests -p 'test_reproduce_skipped.py' -v
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit, Reply  # noqa: E402 - after the sys.path insert
from loop_fixture import LoopFixture, StubProvider, a_task  # noqa: E402 - as above
from test_reproduce_first import BUG_BODY  # noqa: E402 - after the sys.path insert

DECLARED = "OUTCOME: NOT_REPRODUCED"
VERIFY = "python3 -m unittest discover -s tests -p 'test_*.py'"
REASON = "the container isn't available: set REPRO_GATE_UNDER_TEST=1"
EXISTING = """import unittest


class Existing(unittest.TestCase):
    def test_rename_keeps_the_name_on_the_base(self):
        self.assertTrue(open("README.md").read())
"""
TEST = """import os
import unittest


class Modal(unittest.TestCase):
    {gate}
    def test_rename_keeps_the_name(self):
        self.assertEqual(open("app.txt").read() if os.path.exists("app.txt")
                         else "{base}", "fixed\\n")
"""
GATE = (f'@unittest.skipUnless(os.environ.get("REPRO_GATE_UNDER_TEST") == "1",'
        f' "{REASON}")')
GATED = TEST.format(gate=GATE, base="lost")
FAILS = TEST.format(gate="", base="lost")
PASSES = TEST.format(gate="", base="fixed\\n")
MODULE_GATED = f"""import os
import unittest


def setUpModule():
    if os.environ.get("REPRO_GATE_UNDER_TEST") != "1":
        raise unittest.SkipTest("{REASON}")


class Modal(unittest.TestCase):
    def test_rename_keeps_the_name(self):
        self.assertTrue(os.path.exists("app.txt"))

    def test_rename_twice_keeps_the_name(self):
        self.assertTrue(os.path.exists("app.txt"))
"""
IMPORT_GATED = f"""import os
import unittest

if os.environ.get("REPRO_GATE_UNDER_TEST") != "1":
    raise unittest.SkipTest("{REASON}")


class Modal(unittest.TestCase):
    def test_rename_keeps_the_name(self):
        self.assertTrue(os.path.exists("app.txt"))
"""
PYTEST_GATED = f"""import os

import pytest


@pytest.mark.skipif(os.environ.get("REPRO_GATE_UNDER_TEST") != "1",
                    reason="{REASON}")
def test_rename_keeps_the_name():
    assert os.path.exists("app.txt")
"""
SAME_NAME_GATED = EXISTING + f"""
import os


@unittest.skipUnless(os.environ.get("REPRO_GATE_UNDER_TEST") == "1",
                     "{REASON}")
class Renamed(unittest.TestCase):
    def test_rename_keeps_the_name_on_the_base(self):
        self.assertTrue(open("app.txt").read())
"""
MIXIN_PASSES = EXISTING + """

class _Renames:
    def test_rename_keeps_the_name(self):
        self.assertTrue(open("README.md").read())


class Renaming(_Renames, unittest.TestCase):
    pass
"""
FIX = Commit("fix the modal", path="app.txt", body="fixed\n")
CHECKED = Reply("The new test renames a guest the way the ticket reports;"
                " only tests changed.\nVERDICT: PASS")


def reproducing(body, path="tests/test_modal.py"):
    return Commit("test: reproduce the modal", path=path, body=body)


class Declare(Commit):

    def play(self, cwd, turn):
        return f"{super().play(cwd, turn)}\nIt passes on main.\n{DECLARED}"


class ReproduceSkippedTests(LoopFixture):

    def setUp(self):
        super().setUp()
        (self.target / ".gitignore").write_text("__pycache__/\n")
        (self.target / "tests" / "test_existing.py").write_text(EXISTING)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "a running test")
        self.base = self.git("rev-parse", "main").strip()

    def run_bug(self, *script, verify=VERIFY):
        task = dict(a_task(), body=BUG_BODY, budget_min=15, verify=verify)
        return self.loop(*script, provider=StubProvider(task))[0]

    def events(self, kind):
        return [json.loads(payload) for (payload,) in self.read(
            f"SELECT payload FROM runEvents WHERE kind = '{kind}'")]

    def test_new_tests_skipped_on_the_base_get_a_fix_round_not_a_park(self):
        fake = self.run_bug(reproducing(GATED), FIX, APPROVE)

        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        self.assertIn("add a reproducing test that runs without that gate",
                      fake.turns[1].goal)
        self.assertIn(REASON, fake.turns[1].goal)
        self.assertEqual(self.events("reproduce_skipped"), [{"skipped": [
            {"test": "tests/test_modal.py::Modal::test_rename_keeps_the_name",
             "reason": REASON}]}])
        self.assertEqual(self.events("not_reproduced"), [])
        self.assertEqual(self.read("SELECT parkKind FROM runs"), [(None,)])

    def test_a_module_level_gate_skipping_two_new_tests_gets_a_fix_round(self):
        fake = self.run_bug(reproducing(MODULE_GATED), FIX, APPROVE)

        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        self.assertEqual(self.events("reproduce_skipped"), [{"skipped": [
            {"test": "tests/test_modal.py::Modal::test_rename_keeps_the_name",
             "reason": REASON},
            {"test":
             "tests/test_modal.py::Modal::test_rename_twice_keeps_the_name",
             "reason": REASON}]}])
        self.assertEqual(self.events("not_reproduced"), [])

    def test_a_module_skipped_at_import_names_its_reason(self):
        fake = self.run_bug(reproducing(IMPORT_GATED), FIX, APPROVE)

        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        self.assertEqual(self.events("reproduce_skipped"), [{"skipped": [
            {"test": "tests/test_modal.py::Modal::test_rename_keeps_the_name",
             "reason": REASON}]}])

    def test_new_tests_the_verify_never_collects_get_a_fix_round(self):
        fake = self.run_bug(reproducing(FAILS, path="tests/modal_check.py"),
                            FIX, APPROVE)

        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        ((skipped,),) = [event["skipped"]
                         for event in self.events("reproduce_skipped")]
        self.assertEqual(skipped["test"],
                         "tests/modal_check.py::Modal::test_rename_keeps_the_name")
        self.assertIn("not collected", skipped["reason"])
        self.assertEqual(self.events("not_reproduced"), [])

    def test_a_new_test_the_verify_s_own_k_filter_excludes_is_not_collected(self):
        fake = self.run_bug(reproducing(FAILS), FIX, APPROVE,
                            verify=f"{VERIFY} -k on_the_base")

        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        ((skipped,),) = [event["skipped"]
                         for event in self.events("reproduce_skipped")]
        self.assertIn("not collected", skipped["reason"])

    def test_a_quiet_verify_still_shows_which_new_tests_skipped(self):
        fake = self.run_bug(
            reproducing(GATED), FIX, APPROVE,
            verify="python3 -m unittest discover -q -s tests -p 'test_*.py'")

        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        self.assertEqual(self.events("reproduce_skipped"), [{"skipped": [
            {"test": "tests/test_modal.py::Modal::test_rename_keeps_the_name",
             "reason": REASON}]}])

    def test_a_skipped_new_test_named_like_a_passing_one_is_still_skipped(self):
        fake = self.run_bug(
            reproducing(SAME_NAME_GATED, path="tests/test_existing.py"),
            FIX, APPROVE)

        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        self.assertEqual(self.events("reproduce_skipped"), [{"skipped": [
            {"test": "tests/test_existing.py::Renamed::"
                     "test_rename_keeps_the_name_on_the_base",
             "reason": REASON}]}])

    @unittest.skipIf(subprocess.run(["python3", "-c", "import pytest"],
                                    capture_output=True).returncode,
                     "pytest is not installed for python3")
    def test_a_pytest_verify_whose_new_test_skips_gets_a_fix_round(self):
        fake = self.run_bug(reproducing(PYTEST_GATED), FIX, APPROVE,
                            verify="python3 -m pytest -q tests")

        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        self.assertEqual(self.events("reproduce_skipped"), [{"skipped": [
            {"test": "tests/test_modal.py::test_rename_keeps_the_name",
             "reason": REASON}]}])

    def test_a_module_skipped_at_import_under_a_package_names_its_reason(self):
        (self.target / "tests" / "__init__.py").write_text("")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "tests as a package")
        fake = self.run_bug(reproducing(IMPORT_GATED), FIX, APPROVE,
                            verify="python3 -m unittest discover -s tests -t ."
                                   " -p 'test_*.py'")

        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        self.assertEqual(self.events("reproduce_skipped"), [{"skipped": [
            {"test": "tests/test_modal.py::Modal::test_rename_keeps_the_name",
             "reason": REASON}]}])

    def test_a_new_mixin_test_that_runs_and_passes_on_the_base_still_parks(self):
        fake = self.run_bug(
            reproducing(MIXIN_PASSES, path="tests/test_existing.py"), CHECKED)

        self.assertEqual(fake.roles, ["implement", "adjudicate"])
        self.assertEqual(self.read("SELECT parkKind FROM runs"),
                         [("not_reproduced",)])
        self.assertEqual(self.events("reproduce_skipped"), [])

    def test_a_redeclared_fix_whose_tests_skip_again_is_reviewed_not_parked(self):
        fake = self.run_bug(reproducing(GATED),
                            Declare("test the modal again",
                                    path="tests/test_modal_again.py",
                                    body=GATED),
                            APPROVE)

        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        self.assertEqual(len(self.events("reproduce_skipped")), 2)
        self.assertEqual(self.events("not_reproduced"), [])

    def test_new_tests_that_run_and_pass_on_the_base_still_park(self):
        fake = self.run_bug(reproducing(PASSES), CHECKED)

        self.assertEqual(fake.roles, ["implement", "adjudicate"])
        self.assertEqual(self.read("SELECT parkKind FROM runs"),
                         [("not_reproduced",)])
        self.assertEqual(self.events("reproduce_skipped"), [])

    def test_new_tests_that_run_and_fail_on_the_base_proceed_as_reproduced(self):
        fake = self.run_bug(reproducing(FAILS), FIX, APPROVE)

        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        self.assertEqual(len(self.events("reproduced")), 1)
        self.assertEqual(self.read("SELECT status FROM tickets"), [("merged",)])


if __name__ == "__main__":
    unittest.main()
