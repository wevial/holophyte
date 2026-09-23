"""A reported defect the implementer could not reproduce parks on the
maintainer instead of burning review rounds and a strike (KO-657).

The implementer ends its reply with `OUTCOME: NOT_REPRODUCED`; the loop
verifies, then puts one evidence check to the adjudicate seat as round 1.
PASS parks the run `not_reproduced`; FAIL is a normal round with a fix turn.
Fake agents script the turns; the repository, worktree, verify and store are real.
Run: python3 -m unittest tests.test_not_reproduced -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    REQUEST_CHANGES,
    Commit,
    Idle,
    Reply,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    LoopFixture,
    StubProvider,
    a_task,
)

import holophyte.board  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above

# Spelled out rather than imported: the loop's parse is what is under test.
DECLARED = "OUTCOME: NOT_REPRODUCED"
REPRODUCED = Reply("The new test opens the rename-guest modal from the real"
                   " guest record; only tests changed.\nVERDICT: PASS")
REASON = "The added test never opens the rename-guest modal."
REFUSED = Reply(f"{REASON}\nVERDICT: FAIL")


class Declare(Commit):
    """An implementer turn that commits a test and declares the report not
    reproduced on its reply's last line."""

    def play(self, cwd, turn):
        return f"{super().play(cwd, turn)}\nThe test passes on main.\n{DECLARED}"


class NotReproducedTests(LoopFixture):

    def assert_parked_on_maintainer(self):
        head = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(self.read(
            "SELECT phase, parkKind, candidateSha, outcome FROM runs"),
            [("awaiting_merge_approval", "not_reproduced", head, None)])
        ((status, question),) = self.read(
            "SELECT status, blockedQuestion FROM tickets")
        self.assertEqual(status, "blocked_on_operator")
        first = question.splitlines()[0]
        self.assertTrue(first.startswith("not reproduced:"), first)
        self.assertIn(head[:12], first)
        self.assertIn(self.base[:12], first)
        self.assertIn("--approve", question)
        self.assertIn("--requeue", question)
        ((payload,),) = self.read(
            "SELECT payload FROM runEvents WHERE kind = 'not_reproduced'")
        self.assertEqual(json.loads(payload),
                         {"base": self.base, "candidate": head})
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        self.assertEqual(holophyte.board.failure_history(conn, 1), [])

    def test_a_passing_evidence_check_parks_without_a_strike(self):
        fake, _ = self.loop(Declare("test the modal"), REPRODUCED)

        self.assertEqual(fake.roles, ["implement", "adjudicate"])
        self.assertIn(DECLARED, fake.turns[0].goal)
        self.assertEqual(self.read("SELECT round, verdict FROM reviewRounds"),
                         [(1, "pass")])
        self.assert_parked_on_maintainer()

    def test_a_failed_check_is_round_one_and_a_redeclared_fix_is_checked_again(self):
        fake, _ = self.loop(Declare("test the modal"), REFUSED,
                            Declare("open the modal in the test"), REPRODUCED)

        self.assertEqual(fake.roles,
                         ["implement", "adjudicate", "implement", "adjudicate"])
        ((rnd, verdict, findings),) = self.read(
            "SELECT round, verdict, findings FROM reviewRounds WHERE round = 1")
        self.assertEqual((rnd, verdict), (1, "changes_requested"))
        self.assertEqual([f["message"] for f in json.loads(findings)], [REASON])
        self.assertIn(REASON, fake.turns[2].goal)
        self.assert_parked_on_maintainer()

    def test_a_fix_without_the_declaration_goes_to_an_ordinary_review(self):
        fake, _ = self.loop(Declare("test the modal"), REFUSED,
                            Commit("fix the modal"), APPROVE)

        self.assertEqual(fake.roles,
                         ["implement", "adjudicate", "implement", "review"])
        self.assertIn(REASON, fake.turns[2].goal)
        self.assertIn("READ-ONLY code reviewer", fake.turns[3].goal)
        self.assertEqual(self.read("SELECT round, verdict FROM reviewRounds"),
                         [(1, "changes_requested"), (2, "pass")])
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM runEvents WHERE kind = 'not_reproduced'"),
            [(0,)])

    def test_a_one_round_cap_still_gets_the_ordinary_round_two(self):
        self.configure("[loop]\nreview_rounds = 1\nreview_rounds_max = 1\n")
        fake, _ = self.loop(Declare("test the modal"), REFUSED,
                            Commit("fix the modal"), REQUEST_CHANGES,
                            Commit("fix again"), Reply("Mergeable.\nVERDICT: PASS"))

        self.assertEqual(fake.roles, ["implement", "adjudicate", "implement",
                                      "review", "implement", "adjudicate"])
        self.assertIn("READ-ONLY code reviewer", fake.turns[3].goal)
        self.assertEqual(self.read("SELECT round, verdict FROM reviewRounds"),
                         [(1, "changes_requested"), (2, "changes_requested"),
                          (3, "pass")])

    def test_a_declaration_whose_verify_fails_is_set_aside(self):
        provider = StubProvider(dict(a_task(), verify="false"))
        fake, _ = self.loop(Declare("test the modal"), REQUEST_CHANGES, Idle(),
                            provider=provider)

        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        self.assertIn("READ-ONLY code reviewer", fake.turns[1].goal)
        ((summary,),) = self.read("SELECT summary FROM runEvents"
                                  " WHERE kind = 'not_reproduced_set_aside'")
        self.assertIn("set aside: verify failed", summary)
        self.assertEqual(self.read("SELECT round, verdict FROM reviewRounds"),
                         [(1, "changes_requested")])


if __name__ == "__main__":
    unittest.main()
