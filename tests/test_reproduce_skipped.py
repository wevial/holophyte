"""The evidence check counts the candidate's new tests before judging them.

A bug ticket's candidate whose new tests all skip where the check runs goes
to a fix round asking for a runnable reproduction; one whose tests run is
judged as before. Verify is real unittest in a temporary repository whose
base already has a running test and ignores `__pycache__/`, as a real one does.
Run: python3 -m unittest discover -s tests -p 'test_reproduce_skipped.py' -v
"""
from __future__ import annotations

import json
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
REASON = "needs a container: set REPRO_GATE_UNDER_TEST=1"
EXISTING = """import unittest


class Existing(unittest.TestCase):
    def test_readme_is_there(self):
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

    def run_bug(self, *script):
        task = dict(a_task(), body=BUG_BODY, budget_min=15, verify=VERIFY)
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
            {"test": "tests/test_modal.py::test_rename_keeps_the_name",
             "reason": REASON}]}])
        self.assertEqual(self.events("not_reproduced"), [])
        self.assertEqual(self.read("SELECT parkKind FROM runs"), [(None,)])

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
