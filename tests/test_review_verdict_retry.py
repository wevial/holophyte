"""A malformed review gets one reminder, never an implementer fix turn."""
from tests.loop_fixture import BRANCH, Commit, Idle, LoopFixture  # isort: skip

from fake_agent import APPROVE, REQUEST_CHANGES, Reply  # noqa: E402


class ReviewVerdictRetryTests(LoopFixture):
    def test_missing_verdict_then_approval_reaches_merge_gate(self):
        first = Reply("CRITERION 1: not met — stale first assessment\n"
                      + "review detail " * 1100)
        fake, _ = self.loop(Commit(), first, APPROVE)
        reviews = [t for t in fake.turns if t.role == "review"]
        self.assertEqual(len(reviews), 2)
        self.assertTrue(reviews[1].goal.startswith(reviews[0].goal))
        self.assertIn("nothing after it", reviews[1].goal)
        ((verdict, findings),) = self.read(
            "SELECT verdict, findings FROM reviewRounds")
        self.assertEqual(verdict, "pass")
        self.assertIn("first reply (no verdict):", findings)
        self.assertIn("characters cut", findings)
        self.assertIn("reviewing -> merge_gate", self.transitions())

    def test_two_missing_verdicts_fail_as_infra_without_fix(self):
        first = Reply("CRITERION 1: met — tests/test_thing.py::test_it_works\n"
                      "The change looks complete.")
        output = self.main_output(Commit(), first, Reply("Still no verdict."))
        fake = self.last_fake
        self.assertEqual([t.role for t in fake.turns],
                         ["implement", "review", "review"])
        self.assertNotIn("reviewing -> addressing", self.transitions())
        ((verdict, findings),) = self.read(
            "SELECT verdict, findings FROM reviewRounds")
        self.assertEqual(verdict, "error")
        self.assertIn("first reply (no verdict):", findings)
        self.assertIn("Still no verdict.", findings)
        sha = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(self.read(
            "SELECT outcome, outcomeClass, outcomeReason FROM runs"),
            [("failed", "infra", "reviewer returned no verdict line twice; "
              f"candidate preserved at {sha}")])
        self.assertIn("[holo2] round 1: reviewer returned no verdict line twice",
                      output)
        self.assertNotIn("Traceback", output)

    def test_request_changes_calls_reviewer_once_and_addresses(self):
        fake, _ = self.loop(Commit(), REQUEST_CHANGES, Idle())
        self.assertEqual([t.role for t in fake.turns],
                         ["implement", "review", "implement"])
        self.assertIn("reviewing -> addressing", self.transitions())
