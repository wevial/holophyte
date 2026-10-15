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
from loop_fixture import BRANCH, MergeModeFixture  # noqa: E402

NEW_FINDING = "Blocker: src/loader.py:4 drops the empty-input default."
OTHER_FINDING = "Blocker: src/parser.py:9 reads past the end of the buffer."


def changes(finding):
    return Reply(f"{finding}\n"
                 "CRITERION 1: met — tests/test_thing.py::test_it_works\n"
                 "VERDICT: REQUEST_CHANGES")


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

    def rejected_fix(self, merge, *after):
        self.configure('[merge]\nmode = "pr"\n' + merge)
        self.fake_route(states=[self.pr_state([self.DEFECT]), self.pr_state()])
        return self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                         Reply("THREAD 1: ADDRESS -- a real crash"),
                         Commit("fix: default load()"), changes(NEW_FINDING),
                         *after, provider=self.provider())[0]

    def automatic_rounds(self):
        return self.read("SELECT count(*) FROM runEvents"
                         " WHERE kind = 'covering_fix_round'")[0][0]

    def merges(self):
        return [v for kind, v in self.api_calls() if kind == "merge"]

    def test_a_repeated_finding_after_an_automatic_round_parks(self):
        fake = self.rejected_fix('approve = "auto"\n',
                                 Commit("fix: keep the empty-input default"),
                                 changes(NEW_FINDING))

        self.assertEqual(fake.roles[4:], ["implement", "review", "implement",
                                          "review"])
        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertIn(f"the review of the fix at {fixed[:12]} repeated its"
                      " findings", self.question())
        self.assertEqual(self.read("SELECT phase, approvedSha FROM runs"),
                         [("awaiting_merge_approval", None)])
        self.assertEqual(self.automatic_rounds(), 1)
        self.assertFalse(self.merges())

    def test_new_findings_past_covering_fix_rounds_park_as_used_up(self):
        fake = self.rejected_fix('covering_fix_rounds = 1\n',
                                 Commit("fix: keep the empty-input default"),
                                 changes(OTHER_FINDING))

        self.assertEqual(fake.roles.count("implement"), 4)
        self.assertIn("the automatic fix rounds are used up ([merge]"
                      " covering_fix_rounds = 1)", self.question())
        self.assertEqual(self.automatic_rounds(), 1)
        self.assertFalse(self.merges())

    def test_zero_covering_fix_rounds_parks_on_the_first_rejection(self):
        fake = self.rejected_fix('covering_fix_rounds = 0\n')

        self.assertEqual(fake.roles[4:], ["implement", "review"])
        self.assertIn("asked for changes; not merged", self.question())
        self.assertEqual(self.automatic_rounds(), 0)

    def test_human_approval_parks_the_approved_automatic_fix_unmerged(self):
        self.rejected_fix('approve = "human"\nreview_fixes = true\n',
                          Commit("fix: keep the empty-input default"), APPROVE,
                          Idle(""))

        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertIn("waiting for a human to say merge", self.question())
        self.assertEqual(self.read("SELECT phase, approvedSha FROM runs"),
                         [("awaiting_merge_approval", fixed)])
        self.assertEqual(self.automatic_rounds(), 1)
        self.assertFalse(self.merges())


if __name__ == "__main__":
    unittest.main()
