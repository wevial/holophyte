"""A witness pass runs an open story's witnesses at main's tip as it stands
on main, once per tip unless run by hand, reruns a red that was green at an
earlier commit, notes a changed verdict on the parent, and runs nothing for
a held project.

Run: python3 -m unittest discover -s tests -p 'test_witness_pass.py' -v
"""
import shlex
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from story_fixture import WITNESS_FILE, witness_path  # noqa: E402

import linear_provider  # noqa: E402
import store  # noqa: E402
from holophyte.witness import main_tip, witness_pass  # noqa: E402
from store.stories import witness_ledger  # noqa: E402
from tests.test_cli_approve_story import (  # noqa: E402
    FAILS_AN_ASSERTION,
    LEDGER,
    ApproveStoryFixture,
)
from tests.test_cli_native_update import NATIVE, no_linear  # noqa: E402
from tests.test_witness_runner import (  # noqa: E402
    PASSES,
    PYTHON,
    WitnessRunnerFixture,
    commit_file,
)

FAILS_ONCE = """import os
import unittest

MARKER = {marker!r}


class W1Tests(unittest.TestCase):
    def test_sorted(self):
        if not os.path.exists(MARKER):
            open(MARKER, "w").close()
            self.fail("first run")
"""


class WitnessPassCliTests(ApproveStoryFixture):
    def setUp(self):
        patcher = patch.object(linear_provider, "_gql", no_linear)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repository(NATIVE)
        self.write("orders-csv", [FAILS_AN_ASSERTION] * 2)
        status, lines = self.cli("--file-story", "orders-csv")
        self.assertEqual(status, 0, lines)

    def test_a_hand_run_pass_prints_and_ledgers_each_verdict_at_the_tip(self):
        status, lines = self.cli("--approve-story", "NAT-1", "--revision",
                                 str(self.revision("NAT-1")), "--note", "ok")
        self.assertEqual(status, 0, lines)
        tip = commit_file(self.target, witness_path(1),
                          WITNESS_FILE.format(n=1), "w1 lands")

        status, lines = self.cli("--witness-pass", "NAT-1")

        self.assertEqual(status, 0, lines)
        self.assertEqual(lines, [f"[holo2] witness pass at {tip}:"
                                 " W1 green, W2 absent"])
        self.assertEqual(self.store(LEDGER)[-2:], [
            ("W1", tip, "green", None, "operator"),
            ("W2", tip, "absent", None, "operator")])

    def test_a_planned_story_exits_1_and_runs_nothing(self):
        status, lines = self.cli("--witness-pass", "NAT-1")

        self.assertEqual(status, 1)
        self.assertIn("planned, not approved or parked", lines[0])
        self.assertEqual(self.store(LEDGER), [])


class WitnessPassTests(WitnessRunnerFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.target = self.project()

    def story(self, w1_command=None):
        self.approve([
            ("W1", "tests/test_w1.py",
             w1_command or f"{PYTHON} -m unittest tests.test_w1", PASSES),
            ("W2", "tests/test_w2.py", f"{PYTHON} -m unittest tests.test_w2",
             PASSES)])

    def at(self, sha):
        return [(row.witnessKey, row.verdict, row.verifier)
                for row in witness_ledger(self.conn, self.story_id)
                if row.mainSha == sha]

    def test_a_tip_the_ledger_holds_runs_again_only_by_hand(self):
        self.story()
        tip = commit_file(self.repo, "tests/test_w1.py", PASSES, "w1 lands")
        witness_pass(self.target, self.conn, self.story_id, "operator")

        self.assertEqual(
            witness_pass(self.target, self.conn, self.story_id, "loop"), [])
        self.assertEqual(len(witness_pass(
            self.target, self.conn, self.story_id, "operator")), 2)

        new_tip = commit_file(self.repo, "README.md", "more\n", "docs")
        rows = witness_pass(self.target, self.conn, self.story_id, "loop")

        self.assertEqual(len(self.at(tip)), 4)
        self.assertEqual(self.at(new_tip), [("W1", "green", "loop"),
                                            ("W2", "absent", "loop")])
        self.assertEqual([row.mainSha for row in rows], [new_tip] * 2)

    def test_a_red_after_a_green_is_rerun_once_and_both_rows_kept(self):
        self.story()
        commit_file(self.repo, "tests/test_w1.py", PASSES, "w1 lands")
        witness_pass(self.target, self.conn, self.story_id, "loop")
        tip = commit_file(self.repo, "tests/test_w1.py", FAILS_AN_ASSERTION,
                          "w1 breaks")

        witness_pass(self.target, self.conn, self.story_id, "loop")

        self.assertEqual(self.at(tip), [("W1", "red", "loop"),
                                        ("W2", "absent", "loop"),
                                        ("W1", "red", "loop")])

    def test_a_flaky_red_is_rerun_and_its_green_is_the_verdict(self):
        self.story()
        commit_file(self.repo, "tests/test_w1.py", PASSES, "w1 lands")
        witness_pass(self.target, self.conn, self.story_id, "loop")
        tip = commit_file(self.repo, "tests/test_w1.py",
                          FAILS_ONCE.format(marker=str(self.marker)),
                          "w1 flakes")

        witness_pass(self.target, self.conn, self.story_id, "loop")

        self.assertEqual([row for row in self.at(tip) if row[0] == "W1"],
                         [("W1", "red", "loop"), ("W1", "green", "loop")])
        self.assertEqual(
            [(row.witnessKey, row.verdict)
             for row in witness_ledger(self.conn, self.story_id, tip)],
            [("W1", "green"), ("W2", "absent")])

    def test_a_held_project_runs_no_command_and_appends_nothing(self):
        self.story(f"touch {shlex.quote(str(self.marker))} &&"
                   f" {PYTHON} -m unittest tests.test_w1")
        commit_file(self.repo, "tests/test_w1.py", PASSES, "w1 lands")
        store.hold(self.conn, 1, "maintenance")

        rows = witness_pass(self.target, self.conn, self.story_id, "loop")

        self.assertEqual(rows, [])
        self.assertEqual(witness_ledger(self.conn, self.story_id), [])
        self.assertFalse(self.marker.exists())

    def test_a_changed_verdict_notes_the_parent_once_per_tip(self):
        self.story()
        previous = main_tip(self.target)
        witness_pass(self.target, self.conn, self.story_id, "loop")
        tip = commit_file(self.repo, "tests/test_w1.py", PASSES, "w1 lands")

        witness_pass(self.target, self.conn, self.story_id, "loop")
        witness_pass(self.target, self.conn, self.story_id, "operator")

        notes = self.conn.execute(
            "SELECT text FROM ticketNotes WHERE ticketId = ?",
            (self.story_id,)).fetchall()
        self.assertEqual(notes, [(f"Witness W1 is green at {tip}, was absent"
                                  f" at {previous}.",)])


if __name__ == "__main__":
    unittest.main()
