"""A bot's outside-diff findings in a review body, through the pass."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit, Idle, Reply  # noqa: E402
from loop_fixture import MergeModeFixture  # noqa: E402

REVIEW = (HERE / "fixtures" / "pullrequest"
          / "coderabbit_outside_diff_review.md").read_text()
TITLE = "Retry loop never resets its backoff after a success."


class ReviewBodyFindingTests(MergeModeFixture):
    def with_review(self, state, author=("coderabbitai", "Bot")):
        login, kind = author
        state["data"]["repository"]["pullRequest"]["reviews"] = {"nodes": [{
            "id": "PRR_1", "author": {"login": login, "__typename": kind},
            "body": REVIEW, "url": f"{self.URL}#pullrequestreview-1"}]}
        return state

    def test_outside_diff_finding_is_adjudicated_before_a_ready_park(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route(states=[self.with_review(self.pr_state()),
                                self.pr_state()])
        fake, _ = self.loop(Commit("initial work"), APPROVE, Idle(""),
                            Reply("THREAD 1: ADDRESS -- reset the backoff"),
                            Commit("reset the backoff"),
                            provider=self.provider())
        self.assertIn("adjudicate", fake.roles)
        brief = fake.turns[fake.roles.index("adjudicate")].goal
        self.assertIn("src/app.py:40", brief)
        self.assertIn(TITLE, brief)


if __name__ == "__main__":
    unittest.main()
