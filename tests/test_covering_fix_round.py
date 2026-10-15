"""A babysit covering review that asks for new changes gets an automatic fix round."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
sys.path.insert(0, str(HERE))
import babysit_fixture as cases  # noqa: E402
from fake_agent import APPROVE, Commit, Idle, Reply  # noqa: E402
from loop_fixture import MergeModeFixture  # noqa: E402

NEW_FINDING = "Blocker: src/loader.py:4 drops the empty-input default."


class CoveringFixRoundTests(cases.BabysitHelpers, MergeModeFixture):

    def test_a_new_finding_on_the_fix_gets_an_automatic_round_and_merges(self):
        self.configure('[merge]\nmode = "pr"\napprove = "auto"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]), self.pr_state()])
        new_changes = Reply(f"{NEW_FINDING}\n"
                            "CRITERION 1: met — tests/test_thing.py::test_it_works\n"
                            "VERDICT: REQUEST_CHANGES")

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: ADDRESS -- a real crash"),
                            Commit("fix: default load()"), new_changes,
                            Commit("fix: keep the empty-input default"), APPROVE,
                            Idle(""), provider=self.provider())

        self.assertEqual(fake.roles[4:7], ["implement", "review", "implement"])
        self.assertIn("src/loader.py:4 drops the empty-input default",
                      fake.turns[6].goal)
        self.assertIn("Fix note from the review of the fix at", fake.turns[6].goal)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual(self.read("SELECT count(*) FROM runEvents"
                                   " WHERE kind = 'covering_fix_round'"), [(1,)])
        ledger = [text for text, in self.read("SELECT text FROM ledger")]
        self.assertTrue(any("Automatic fix round 1 of 2" in text for text in ledger),
                        ledger)


if __name__ == "__main__":
    unittest.main()
