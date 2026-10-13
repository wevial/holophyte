"""The terminal adjudication's criteria gate, driven end to end with fake agents.

Run: python3 -m unittest discover -s tests -p 'test_adjudication_criteria.py' -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import IMPLEMENT, PASS, REQUEST_CHANGES, Commit, Reply  # noqa: E402
from loop_fixture import BRANCH, LoopFixture  # noqa: E402
from pause_fixture import PauseReply  # noqa: E402

import store  # noqa: E402
from holophyte.loop.stop import command  # noqa: E402
from tests.test_steer import SteerFixture  # noqa: E402
from tests.test_steer_live import Steering  # noqa: E402

CRITERION = "Given the thing, when it runs, then it works"
PROSE_PASS = ("Mergeable.\nCRITERION 1: met — the commit message records "
              "the measurement\nVERDICT: PASS")
LEFT_UNWITNESSED = Reply(
    "Blocker: the measurement is missing.\n"
    "CRITERION 1: unwitnessed — the measurement is not in the pull "
    "request description\nVERDICT: REQUEST_CHANGES")
LEFT_OPEN = ("The newest review round left these criteria unmet or "
             f"unwitnessed:\n1. {CRITERION}\n")


class PausingCommit:
    role = IMPLEMENT

    def __init__(self, db, message):
        self.db, self.commit = db, Commit(message)

    def play(self, cwd, turn):
        reply = self.commit.play(cwd, turn)
        conn = store.open(self.db)
        try:
            ((run,),) = conn.execute(
                "SELECT id FROM runs WHERE endedAt IS NULL").fetchall()
            store.pause(conn, run, "reboot writer")
        finally:
            conn.close()
        return reply


class AdjudicationCriteriaTests(LoopFixture):
    def adjudicate(self, verdict):
        """Two review rounds, the second leaving criterion 1 unwitnessed,
        then the adjudicator's reply `verdict`."""
        fake, _ = self.loop(Commit("first cut"), REQUEST_CHANGES,
                            Commit("fix round 1"), LEFT_UNWITNESSED,
                            Commit("fix round 2"), Reply(verdict))
        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "review", "implement", "adjudicate"])
        return fake

    def assert_failed_naming_criterion_1(self, status):
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertIn(BRANCH, self.branches())
        self.assertIn("fix round 2", self.subjects(BRANCH))
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertTrue(reason.startswith("terminal adjudication: FAIL"), reason)
        self.assertIn(f"criterion 1 {status}", reason)
        ((verdict, findings),) = self.read(
            "SELECT verdict, findings FROM reviewRounds WHERE round = 3")
        self.assertEqual(verdict, "changes_requested")
        self.assertEqual([(f["path"], f["line"]) for f in json.loads(findings)],
                         [("criteria", 1)])

    def test_a_pass_overturning_an_unwitnessed_criterion_with_prose_fails(self):
        self.adjudicate(PROSE_PASS)
        self.assert_failed_naming_criterion_1("unwitnessed")

    def test_a_resumed_adjudication_still_refuses_a_prose_pass(self):
        self.loop(Commit("first cut"), REQUEST_CHANGES,
                  Commit("fix round 1"), LEFT_UNWITNESSED,
                  Commit("fix round 2"), PauseReply(self.db, Reply(PROSE_PASS)))
        self.assertEqual(self.read("SELECT outcome, resumePhase FROM runs"),
                         [("paused", "reviewing")])

        command(self.project, "KO-131", None, resume=True)
        self.loop()

        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("paused",), ("failed",)])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        ((reason,),) = self.read("SELECT outcomeReason FROM runs"
                                 " WHERE outcome = 'failed'")
        self.assertIn("criterion 1 unwitnessed", reason)

    def test_a_run_resumed_after_its_final_fix_still_refuses_a_prose_pass(self):
        self.loop(Commit("first cut"), REQUEST_CHANGES,
                  Commit("fix round 1"), LEFT_UNWITNESSED,
                  PausingCommit(self.db, "fix round 2"))
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("paused",)])

        command(self.project, "KO-131", None, resume=True)
        fake, _ = self.loop(Reply(PROSE_PASS))

        self.assertEqual(fake.roles, ["adjudicate"])
        self.assertIn(LEFT_OPEN, fake.turns[0].goal)
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("paused",), ("failed",)])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        ((reason,),) = self.read("SELECT outcomeReason FROM runs"
                                 " WHERE outcome = 'failed'")
        self.assertIn("criterion 1 unwitnessed", reason)

    def test_a_pass_with_no_criterion_line_fails(self):
        self.adjudicate("Mergeable as it stands.\nVERDICT: PASS")
        self.assert_failed_naming_criterion_1("unwitnessed")

    def test_a_pass_reading_criterion_not_met_fails(self):
        self.adjudicate("Mergeable.\nCRITERION 1: not met — no measurement"
                        "\nVERDICT: PASS")
        self.assert_failed_naming_criterion_1("not met")

    def test_a_pass_naming_an_existing_test_merges(self):
        self.adjudicate("Mergeable.\nCRITERION 1: met — "
                        "tests/test_thing.py::test_it_works\nVERDICT: PASS")
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertIn("fix round 2", self.subjects())

    def test_a_pass_naming_a_missing_test_fails(self):
        self.adjudicate("Mergeable.\nCRITERION 1: met — "
                        "tests/test_thing.py::test_missing\nVERDICT: PASS")
        self.assert_failed_naming_criterion_1("unwitnessed")

    def test_the_prompt_names_the_criterion_left_open_and_the_fail_verdict(self):
        fake = self.adjudicate("Not mergeable.\nVERDICT: FAIL")
        prompt = fake.turns[-1].goal
        self.assertIn(LEFT_OPEN, prompt)
        self.assertIn("the round is FAIL regardless of the verdict line", prompt)
        self.assertNotIn("REQUEST_CHANGES", prompt)


class SteeredAdjudicationTests(SteerFixture):
    def test_an_adjudication_repeated_after_a_steer_still_refuses_a_prose_pass(self):
        fake, _ = self.loop(
            Commit("first cut"), REQUEST_CHANGES,
            Commit("fix round 1"), LEFT_UNWITNESSED,
            Commit("fix round 2"), Steering(self, PASS, "-n", "also log it"),
            Commit("the steer"), Reply(PROSE_PASS))

        self.assertEqual(fake.roles[-3:], ["adjudicate", "implement", "adjudicate"])
        self.assertIn(LEFT_OPEN, fake.turns[-1].goal)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("criterion 1 unwitnessed", reason)


if __name__ == "__main__":
    unittest.main()
