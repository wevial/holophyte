"""A bug ticket's first turn reproduces the bug before any fix (KO-659).

A ticket whose body carries `## Reproduce` opens with a short reproduce turn
on the implementer seat that commits a failing test only; the loop runs the
ticket's verify at that commit. A failure hands the rest to the implement
turn, a pass goes to the not-reproduced evidence check (KO-657), and a turn
that commits nothing leaves the implement turn to run as it always did.
Fake agents script the turns; the repository, worktree, verify and store are real.
Run: python3 -m unittest tests.test_reproduce_first -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    Commit,
    Idle,
    Reply,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    VALID_BODY,
    LoopFixture,
    StubProvider,
    a_task,
)

# Fails once the reproduce turn's test file exists, until the fix lands.
VERIFY = "test ! -e repro.txt || grep -q fixed app.txt"
BUG_BODY = VALID_BODY.replace(
    "## In scope",
    "## Reproduce\n\n1. Run the thing.\n2. It does not work.\n\n"
    "Seen on: the preview deployment at commit abc1234.\n\n## In scope"
).replace("Estimate: 5 min", "Estimate: 15 min")
REPRODUCE = Commit("test: reproduce the thing", path="repro.txt")
PASSES = Reply("The test runs the thing the way the ticket reports and only"
               " tests changed.\nVERDICT: PASS")


def bug_task(verify=VERIFY):
    return dict(a_task(), body=BUG_BODY, budget_min=15, verify=verify)


class ReproduceFirstTests(LoopFixture):

    def run_bug(self, *script, verify=VERIFY):
        return self.loop(*script, provider=StubProvider(bug_task(verify)))[0]

    def test_a_failing_reproduction_hands_its_commit_to_the_implement_turn(self):
        fake = self.run_bug(REPRODUCE,
                            Commit("fix the thing", path="app.txt",
                                   body="fixed\n"),
                            APPROVE)

        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        self.assertEqual(fake.turns[0].timeout, 5 * 60)
        (test_sha,) = self.git("log", "main", "--format=%H", "--fixed-strings",
                               f"--grep={REPRODUCE.message}").split()
        ((summary,),) = self.read(
            "SELECT summary FROM runEvents WHERE kind = 'reproduced'")
        self.assertIn(VERIFY, summary)
        self.assertIn(test_sha, fake.turns[1].goal)
        self.assertEqual(fake.turns[1].timeout, 15 * 60)

    def test_a_reproduction_verify_passes_goes_to_the_evidence_check(self):
        fake = self.run_bug(REPRODUCE, PASSES, verify="echo ok")

        self.assertEqual(fake.roles, ["implement", "adjudicate"])
        self.assertEqual(self.read("SELECT parkKind FROM runs"),
                         [("not_reproduced",)])
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM runEvents WHERE kind = 'reproduced'"), [(0,)])

    def test_a_reproduce_turn_that_commits_nothing_leaves_a_note(self):
        fake = self.run_bug(Idle(),
                            Commit("fix the thing", path="app.txt",
                                   body="fixed\n"),
                            APPROVE, verify="grep -q fixed app.txt")

        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        self.assertEqual(fake.turns[1].timeout, 15 * 60)
        notes = [text for (text,) in self.read(
            "SELECT text FROM ledger WHERE kind = 'note'")]
        self.assertTrue(any("No reproduction was committed" in text
                            for text in notes), notes)


if __name__ == "__main__":
    unittest.main()
