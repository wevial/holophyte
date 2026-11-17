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
from fake_agent import (  # noqa: E402
    IMPLEMENT,
    PASS,
    REQUEST_CHANGES,
    Commit,
    Idle,
    Reply,
)
from loop_fixture import BRANCH, LoopFixture  # noqa: E402
from pause_fixture import PauseReply  # noqa: E402

import store  # noqa: E402
from holophyte.loop.stop import command, resume_paused  # noqa: E402
from tests.test_steer import SteerFixture  # noqa: E402
from tests.test_steer_live import Steering  # noqa: E402

CRITERION = "Given the thing, when it runs, then it works"
PROSE_PASS = ("Mergeable.\nCRITERION 1: met — the commit message records "
              "the measurement\nVERDICT: PASS")
LEFT_UNWITNESSED = Reply(
    "Blocker: the measurement is missing.\n"
    "CRITERION 1: unwitnessed — the measurement is not in the pull "
    "request description\nVERDICT: REQUEST_CHANGES")
MISTYPED = ("CRITERION 1: met \u2014 "
            "tests/test_thing.py::test_it_work")
EXACT = "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works"
LEFT_OPEN = ("The newest review round left these criteria unmet or "
             f"unwitnessed:\nC1. {CRITERION}\n")


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
        self.assertIn(f"criterion C1 {status}", reason)
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
        self.assertIn("criterion C1 unwitnessed", reason)

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
        self.assertIn("criterion C1 unwitnessed", reason)

    def test_a_pass_with_no_criterion_line_is_an_infra_failure(self):
        self.adjudicate("Mergeable as it stands.\nVERDICT: PASS")
        ((klass, reason),) = self.read("SELECT outcomeClass, outcomeReason FROM runs")
        self.assertEqual(klass, "infra")
        self.assertFalse(reason.startswith("terminal adjudication: FAIL"), reason)
        self.assertIn(BRANCH, self.branches())

    def test_a_pass_reading_criterion_not_met_fails(self):
        self.adjudicate("Mergeable.\nCRITERION 1: not met — no measurement"
                        "\nVERDICT: PASS")
        self.assert_failed_naming_criterion_1("not met")

    def test_a_pass_naming_an_existing_test_merges(self):
        self.adjudicate("Mergeable.\nCRITERION 1: met — "
                        "tests/test_thing.py::test_it_works\nVERDICT: PASS")
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertIn("fix round 2", self.subjects())

    def recite(self, restated):
        """The adjudicator's PASS cites a test by a name its file lacks;
        the re-cite turn replies `restated`."""
        return self.recite_after(LEFT_UNWITNESSED, Commit("fix round 2"),
                                 MISTYPED, restated)

    def recite_after(self, review, fix, cited, restated, *after):
        fake, _ = self.loop(Commit("first cut"), REQUEST_CHANGES,
                            Commit("fix round 1"), review, fix,
                            Reply(f"Mergeable.\n{cited}\nVERDICT: PASS"),
                            restated, *after)
        return fake

    def test_a_mistyped_test_name_restated_exactly_passes(self):
        fake = self.recite(Reply(EXACT))

        self.assertEqual(fake.roles[-2:], ["adjudicate", "adjudicate"])
        self.assertIn("- tests/test_thing.py::test_it_works\n",
                      fake.turns[-1].goal)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual(self.read(
            "SELECT verdict FROM reviewRounds WHERE round = 3"), [("pass",)])
        ((entry,),) = self.read(
            "SELECT text FROM ledger WHERE kind = 'adjudication'")
        self.assertIn(f"Cited: {MISTYPED}\n", entry)
        self.assertIn(f"Restated: {EXACT}\n", entry)

    def test_a_restated_name_still_missing_fails_after_two_asks(self):
        fake = self.recite(Reply(
            "CRITERION 1: met \u2014 tests/test_thing.py::test_works"))

        self.assertEqual(fake.roles.count("adjudicate"), 2)
        self.assert_failed_naming_criterion_1("unwitnessed")
        ((findings,),) = self.read(
            "SELECT findings FROM reviewRounds WHERE round = 3")
        self.assertIn("named test not found: tests/test_thing.py::test_works",
                      json.loads(findings)[0]["message"])

    def test_a_restated_name_with_no_test_path_fails(self):
        witnessed = Reply(f"Blocker: tidy up.\n{EXACT}\nVERDICT: REQUEST_CHANGES")
        fake = self.recite_after(witnessed, Commit("fix round 2"), MISTYPED,
                                 Reply("CRITERION 1: met \u2014 test_it_work"))

        self.assertEqual(fake.roles.count("adjudicate"), 2)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)

    def test_a_bare_typo_beside_a_valid_citation_still_fails(self):
        cited = ("CRITERION 1: met \u2014 tests/test_other.py::test_other_works"
                 " and tests/test_thing.py::test_it_work")
        fake = self.recite_after(
            LEFT_UNWITNESSED,
            Commit("fix round 2", path="tests/test_other.py",
                   body="def test_other_works():\n    pass\n"),
            cited, Reply("CRITERION 1: met \u2014 "
                         "tests/test_other.py::test_other_works and test_it_work"))

        self.assertEqual(fake.roles.count("adjudicate"), 2)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)

    def test_a_re_cite_answering_not_met_fails_on_that_answer(self):
        answer = "CRITERION 1: not met \u2014 the test was never added"
        self.recite(Reply(answer))

        self.assert_failed_naming_criterion_1("not met")
        ((entry,),) = self.read(
            "SELECT text FROM ledger WHERE kind = 'adjudication'")
        self.assertIn(f"Restated: {answer}\n", entry)

    def test_a_pause_during_the_first_turn_stops_before_the_re_cite(self):
        paused, _ = self.loop(
            Commit("first cut"), REQUEST_CHANGES, Commit("fix round 1"),
            LEFT_UNWITNESSED, Commit("fix round 2"),
            PauseReply(self.db, Reply(f"Mergeable.\n{MISTYPED}\nVERDICT: PASS")))
        self.assertEqual(paused.roles.count("adjudicate"), 1)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("paused",)])

        command(self.project, "KO-131", None, resume=True)
        resumed, _ = self.loop(Reply(EXACT))

        self.assertEqual(resumed.roles, ["adjudicate"])
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("paused",), ("merged",)])

    def test_the_re_cite_prompt_lists_every_file_the_refused_line_cites(self):
        cited = ("CRITERION 1: met \u2014 tests/test_other.py::test_other_works"
                 " and tests/test_thing.py::test_it_work")
        fake = self.recite_after(
            LEFT_UNWITNESSED,
            Commit("fix round 2", path="tests/test_other.py",
                   body="def test_other_works():\n    pass\n"),
            cited, Reply(f"{EXACT} and tests/test_other.py::test_other_works"),
            Idle())

        recite = fake.turns[fake.roles.index("adjudicate") + 1].goal
        self.assertIn("- tests/test_other.py::test_other_works\n", recite)
        self.assertIn("- tests/test_thing.py::test_it_works\n", recite)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_re_cite_paused_on_resumes_without_asking_again(self):
        self.recite(PauseReply(self.db, Reply(EXACT)))
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("paused",)])

        command(self.project, "KO-131", None, resume=True)
        fake, _ = self.loop()

        self.assertEqual(fake.roles, [])
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("paused",), ("merged",)])

    def test_a_pass_citing_a_file_the_worktree_lacks_fails_unasked(self):
        self.adjudicate("Mergeable.\nCRITERION 1: met \u2014 "
                        "tests/test_absent.py::test_it_works\nVERDICT: PASS")
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
        self.assertIn("criterion C1 unwitnessed", reason)

    def test_a_steered_adjudication_paused_and_resumed_still_refuses_a_prose_pass(self):
        paused, _ = self.loop(
            Commit("first cut"), REQUEST_CHANGES,
            Commit("fix round 1"), LEFT_UNWITNESSED,
            Commit("fix round 2"), Steering(self, PASS, "-n", "also log it"),
            Commit("the steer"),
            Steering(self, Reply(PROSE_PASS), "-n", "and the host", pause=True))
        self.assertEqual(paused.roles[-3:], ["adjudicate", "implement", "adjudicate"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("paused",)])
        resume_paused(self.project, self.conn, self.ticket, "go on")

        resumed, _ = self.loop(Commit("the second steer"), Reply(PROSE_PASS))

        self.assertEqual(resumed.roles, ["implement", "adjudicate"])
        self.assertIn(LEFT_OPEN, resumed.turns[-1].goal)
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("paused",), ("failed",)])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        ((reason,),) = self.read("SELECT outcomeReason FROM runs"
                                 " WHERE outcome = 'failed'")
        self.assertIn("criterion C1 unwitnessed", reason)

    def test_a_resume_whose_raised_cap_runs_a_review_gates_on_that_review(self):
        self.loop(Commit("first cut"), REQUEST_CHANGES,
                  Commit("fix round 1"), Reply(
                      "Blocker: tidy up.\nCRITERION 1: met — "
                      "tests/test_thing.py::test_it_works\n"
                      "VERDICT: REQUEST_CHANGES"),
                  Commit("fix round 2"), Steering(self, PASS, "-n", "also log it"),
                  Commit("the steer"),
                  Steering(self, Reply("Not yet.\nVERDICT: FAIL"), "-n",
                           "and the host", pause=True))
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("paused",)])
        self.configure("[loop]\nreview_rounds = 4\n")
        resume_paused(self.project, self.conn, self.ticket, "go on")

        resumed, _ = self.loop(Commit("the second steer"), LEFT_UNWITNESSED,
                               Commit("fix round 4"), Reply(PROSE_PASS))

        self.assertEqual(resumed.roles, ["implement", "review", "implement",
                                         "adjudicate"])
        self.assertIn(LEFT_OPEN, resumed.turns[-1].goal)
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("paused",), ("failed",)])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        ((reason,),) = self.read("SELECT outcomeReason FROM runs"
                                 " WHERE outcome = 'failed'")
        self.assertIn("criterion C1 unwitnessed", reason)


if __name__ == "__main__":
    unittest.main()
