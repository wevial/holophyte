"""A bot's outside-diff findings in a review body, through the pass."""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit, Idle, Reply  # noqa: E402
from loop_fixture import BRANCH, MergeModeFixture  # noqa: E402

REVIEW = (HERE / "fixtures" / "pullrequest"
          / "coderabbit_outside_diff_review.md").read_text()
TITLE = "Retry loop never resets its backoff after a success."
FINGERPRINT = hashlib.sha1(
    f"coderabbitai\nsrc/app.py\n40-52\n{TITLE}".encode()).hexdigest()[:12]
MARKER = f"<!-- holophyte:review-finding {FINGERPRINT} -->"
QUOTE = (f"> [Review by @coderabbitai](https://github.com/example/repo/pull/7"
         f"#pullrequestreview-1): src/app.py:40-52 (Major): {TITLE}")


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
                            Commit("reset the backoff"), Idle(""),
                            provider=self.provider())
        self.assertIn("adjudicate", fake.roles)
        brief = fake.turns[fake.roles.index("adjudicate")].goal
        self.assertIn("src/app.py:40", brief)
        self.assertIn(TITLE, brief)
        self.assertIn("reset the backoff", fake.turns[4].goal)
        kinds = [kind for kind, _ in self.api_calls()]
        self.assertEqual(kinds.count("conversation"), 1)
        self.assertNotIn("reply", kinds)
        self.assertNotIn("resolve", kinds)
        body = self.answer()
        self.assertTrue(body.startswith(QUOTE + "\n\n"))
        self.assertIn(f"Addressed in {self.git('rev-parse', BRANCH).strip()}",
                      body)
        self.assertTrue(body.endswith(MARKER))

    def answer(self):
        (body,) = [variables["body"] for kind, variables in self.api_calls()
                   if kind == "conversation"]
        self.assertTrue(any("POST repos/example/repo/issues/7/comments" in c
                            for c in self.recorded()))
        return body

    def test_declined_finding_is_answered_on_the_pull_and_left_unresolved(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route(states=[self.with_review(self.pr_state()),
                                self.pr_state()])
        self.loop(Commit("initial work"), APPROVE, Idle(""),
                  Reply("THREAD 1: DECLINE -- the backoff resets in caller()"),
                  provider=self.provider())
        kinds = [kind for kind, _ in self.api_calls()]
        self.assertNotIn("reply", kinds)
        self.assertNotIn("resolve", kinds)
        body = self.answer()
        self.assertIn("Declined: the backoff resets in caller()", body)
        self.assertTrue(body.endswith(MARKER))

    def test_answered_finding_is_not_adjudicated_again(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        state = self.with_review(self.pr_state())
        state["data"]["repository"]["pullRequest"]["comments"] = {"nodes": [
            dict(self.comment(2, ("writer", "User"),
                              f"{QUOTE}\n\nDeclined: no.\n\n{MARKER}"),
                 id="IC_2", viewerDidAuthor=True)]}
        self.fake_route(states=[state])
        fake, _ = self.loop(Commit("initial work"), APPROVE, Idle(""),
                            provider=self.provider())
        self.assertNotIn("adjudicate", fake.roles)
        self.assertIn("waiting for a human to say merge", self.question())

    def test_a_persons_review_makes_no_item(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route(states=[self.with_review(
            self.pr_state(), author=("review-helper", "User"))])
        fake, _ = self.loop(Commit("initial work"), APPROVE, Idle(""),
                            provider=self.provider())
        self.assertNotIn("adjudicate", fake.roles)
        self.assertIn("waiting for a human to say merge", self.question())

    def test_a_listed_bot_login_review_makes_an_item(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       'bot_logins = ["review-helper"]\n')
        self.fake_route(states=[self.with_review(
            self.pr_state(), author=("review-helper", "User"))])
        fake, _ = self.loop(Commit("initial work"), APPROVE, Idle(""),
                            Reply("THREAD 1: HUMAN -- needs the maintainer"),
                            provider=self.provider())
        brief = fake.turns[fake.roles.index("adjudicate")].goal
        self.assertIn(TITLE, brief)


if __name__ == "__main__":
    unittest.main()
