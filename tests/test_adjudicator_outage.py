from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name

from fake_agent import APPROVE, REVIEW_ROLES, Commit, Idle  # noqa: E402
from loop_fixture import MergeModeFixture  # noqa: E402

from holophyte.agents.agent_output import AgentOutput  # noqa: E402

LIMIT = "You've hit your session limit · resets 3:20am (UTC)"


@dataclass(frozen=True)
class FailedTurn:
    """An adjudicator turn whose command printed `text` and exited 1."""

    text: str

    role = REVIEW_ROLES

    def play(self, cwd, turn):
        return AgentOutput(self.text + "\n", "claude -p", exit_code=1)


class AdjudicatorOutageTests(MergeModeFixture):
    def test_a_failed_adjudicator_turn_parks_as_a_route_outage(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT])])

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            FailedTurn(LIMIT), provider=self.provider())

        self.assertEqual(fake.roles,
                         ["implement", "review", "implement", "adjudicate"])
        self.assertEqual(self.read("SELECT phase, parkKind FROM runs"),
                         [("awaiting_merge_approval", "route")])
        question = self.question()
        self.assertIn("the adjudicator turn failed", question)
        self.assertIn(LIMIT, question)
        self.assertNotIn("a thread needs a human's answer", question)
        self.assertEqual(
            [kind for kind, _ in self.api_calls() if kind == "reply"], [])


if __name__ == "__main__":
    unittest.main()
