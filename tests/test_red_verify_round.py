"""An approved review round over a red verify hands its fix turn the failure."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_agent import APPROVE, REQUEST_CHANGES, Commit, Idle  # noqa: E402
from loop_fixture import LoopFixture, StubProvider, a_task  # noqa: E402

RED = "test -e scripted-3.txt"


class RedVerifyRoundTests(LoopFixture):
    TASK = dict(a_task(), verify=RED)

    def test_an_approved_red_round_hands_its_fix_turn_the_failing_verify(self):
        fake, _ = self.loop(Commit("work"), APPROVE, Commit("fix round 1"),
                            APPROVE, provider=StubProvider(self.TASK))

        fix_goal = fake.turns[2].goal
        self.assertIn("[verify] FAILED", fix_goal)
        self.assertIn(RED, fix_goal)
        ((verdict, findings),) = self.read(
            "SELECT verdict, findings FROM reviewRounds WHERE round = 1")
        self.assertEqual(verdict, "changes_requested")
        self.assertTrue(any(RED in f.get("title", "")
                            for f in json.loads(findings)))
        self.assertEqual(fake.roles,
                         ["implement", "review", "implement", "review"])
        ((outcome,),) = self.read("SELECT outcome FROM runs")
        self.assertEqual(outcome, "merged")

    def test_a_fix_turn_that_ignores_a_red_verify_fails_naming_its_command(self):
        self.loop(Commit("work"), APPROVE, Idle(),
                  provider=StubProvider(self.TASK))

        ((reason, kind),) = self.read(
            "SELECT outcomeReason, failureKind FROM runs")
        self.assertEqual(kind, "fix_no_progress")
        self.assertIn("1 findings open", reason)
        self.assertIn(RED, reason)
        self.assertNotIn("(none recorded)", reason)

    def test_a_red_round_that_asked_for_changes_hands_on_both(self):
        fake, _ = self.loop(Commit("work"), REQUEST_CHANGES,
                            Commit("fix round 1"), APPROVE,
                            provider=StubProvider(self.TASK))

        fix_goal = fake.turns[2].goal
        self.assertIn("Blocker: the scripted change is incomplete.", fix_goal)
        self.assertIn("[verify] FAILED", fix_goal)


class GreenVerifyRoundTests(LoopFixture):
    def test_an_approved_green_round_merges_with_no_findings(self):
        fake, _ = self.loop(Commit("work"), APPROVE,
                            provider=StubProvider(dict(a_task(), verify="echo ok")))

        self.assertEqual(fake.roles, ["implement", "review"])
        ((verdict, findings),) = self.read(
            "SELECT verdict, findings FROM reviewRounds WHERE round = 1")
        self.assertEqual((verdict, json.loads(findings)), ("pass", []))
        ((outcome,),) = self.read("SELECT outcome FROM runs")
        self.assertEqual(outcome, "merged")
