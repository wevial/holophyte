"""An implementer that cannot meet a criterion as written declares it with a
`CONTRADICTION: criterion N — reason` line, and the run parks on a question
to the operator instead of going to review.

Fake agents script the turns; the repository, worktree, verify and store are real.
Run: python3 -m unittest discover -s tests -p 'test_implementer_contradiction.py'
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
    REQUEST_CHANGES,
    Commit,
    Idle,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    LoopFixture,
    StubProvider,
    a_task,
)

import holophyte.board.projection  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above

# Spelled out rather than imported: the loop's parse is what is under test.
REASON = "the pr_open facts carry only open_threads"
DECLARED = f"CONTRADICTION: criterion 2 — {REASON}"


def two_criteria():
    return dict(a_task(), criteria=[
        "Given a parked run, when the page renders, then it says parked",
        "Given one bot thread, when the page renders, then it says"
        " 'parked · 1 bot thread'"])


class Declare(Commit):
    """An implement turn that commits a change and declares criterion 2
    unmeetable on its reply's last line."""

    def play(self, cwd, turn):
        return f"{super().play(cwd, turn)}\nCriterion 1 is done.\n{DECLARED}"


class ImplementTurnContradictionTests(LoopFixture):

    def test_a_declared_contradiction_parks_on_the_operator_without_review(self):
        fake, _ = self.loop(Declare("render the parked word"), REQUEST_CHANGES,
                            Idle(), provider=StubProvider(two_criteria()))

        self.assertEqual(fake.roles, ["implement"])
        self.assertEqual(self.read("SELECT round FROM reviewRounds"), [])
        head = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(self.read(
            "SELECT phase, parkKind, candidateSha, outcome FROM runs"),
            [("blocked_on_operator", "contradiction", head, None)])
        ((status, question),) = self.read(
            "SELECT status, blockedQuestion FROM tickets")
        self.assertEqual(status, "blocked_on_operator")
        self.assertEqual(
            question.splitlines()[0],
            f"implementer: criterion 2 cannot be met as written: {REASON}")
        ((payload,),) = self.read(
            "SELECT payload FROM runEvents WHERE kind = 'contradiction'")
        self.assertEqual(json.loads(payload), {
            "turn": "implement", "head": head,
            "criteria": [{"criterion": 2, "reason": REASON}]})
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        self.assertEqual(holophyte.board.projection.failure_history(conn, 1), [])


if __name__ == "__main__":
    unittest.main()
