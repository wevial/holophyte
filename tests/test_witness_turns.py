"""A gap the witness pre-check finds before round 1 goes back to the
implementer, up to two turns a run, before the review sees what is left.

Verify is real unittest in a temporary repository; the agents are scripted.
Run: python3 -m unittest discover -s tests -p 'test_witness_turns.py' -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after sys.path
    APPROVE,
    Commit,
    FakeAgent,
    Idle,
    Reply,
)
from loop_fixture import LoopFixture, StubProvider, a_task  # noqa: E402 - as above

import holophyte.loop.implement  # noqa: E402 - as above
import store  # noqa: E402 - as above
from holophyte.loop.stop import command  # noqa: E402 - as above

VERIFY = "python3 -m unittest discover -s tests -p 'test_*.py'"
CRITERION = "A test in `tests/test_gate.py` witnesses it."
GAP = ("criterion C1 names `tests/test_gate.py`; its tests ran only skipped"
       " in verify (needs a browser)")
RUNS = """import unittest


class Runs(unittest.TestCase):
    def test_it_runs(self):
        self.assertTrue(True)
"""
GATED = """import unittest


class Gate(unittest.TestCase):
    @unittest.skip("needs a browser")
    def test_it_gates(self):
        pass
{extra}"""
OPENED = """import unittest


class Gate(unittest.TestCase):
    def test_it_gates(self):
        self.assertTrue(True)
"""
BROKEN = """import unittest


class Broken(unittest.TestCase):
    def test_it_breaks(self):
        self.fail("broken by the witness turn")
"""

BOTH_MET = Reply("CRITERION 1: met \u2014 tests/test_gate.py::test_it_gates\n"
                 "CRITERION 2: met \u2014 tests/test_runs.py::test_it_runs\n"
                 "VERDICT: APPROVE")


def gated(n=0):
    return Commit(f"gated test {n}", path="tests/test_gate.py",
                  body=GATED.format(extra=f"# revision {n}\n" if n else ""))


class PausingCommit(Commit):

    def __init__(self, db, step):
        super().__init__(step.message, step.path, step.body)
        self.db = db

    def play(self, cwd, turn):
        reply = super().play(cwd, turn)
        conn = store.open(self.db)
        try:
            ((run,),) = conn.execute(
                "SELECT id FROM runs WHERE endedAt IS NULL").fetchall()
            store.pause(conn, run, "reboot writer")
        finally:
            conn.close()
        return reply


class WitnessTurns(LoopFixture):

    def setUp(self):
        super().setUp()
        (self.target / ".gitignore").write_text("__pycache__/\n")
        (self.target / "tests" / "test_runs.py").write_text(RUNS)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "a running test")
        self.base = self.git("rev-parse", "main").strip()

    def run_task(self, *script, criteria=(CRITERION,), fake=None,
                 verify=VERIFY):
        task = dict(a_task(), verify=verify, budget_min=15,
                    criteria=list(criteria))
        return self.loop(*script, fake=fake, provider=StubProvider(task))[0]

    def summaries(self, kind):
        return [summary for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = '"
            f"{kind}' ORDER BY seq")]

    def review_goal(self, fake):
        (goal,) = [turn.goal for turn in fake.turns if turn.role == "review"][:1]
        return goal

    def assert_merged_after_round_one(self):
        self.assertEqual(self.read("SELECT round FROM reviewRounds"), [(1,)])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_skipped_named_test_gets_a_witness_turn_before_round_one(self):
        fake = self.run_task(
            gated(), Commit("run the gated test", path="tests/test_gate.py",
                            body=OPENED), APPROVE)

        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        self.assertIn("criterion C1 names", fake.turns[1].goal)
        self.assertIn("ran only skipped in verify", fake.turns[1].goal)
        self.assertEqual(len(self.summaries("witness_turn")), 1)
        first, second = self.summaries("witness_precheck")
        self.assertIn(GAP, first)
        self.assertIn("1 of 1 criteria witnessed", second)
        self.assertNotIn("criterion C1 names", second)
        self.assertNotIn("witness pre-check found these gaps",
                         self.review_goal(fake))
        self.assert_merged_after_round_one()

    def test_two_witness_turns_at_most_then_the_review_sees_the_gap(self):
        fake = self.run_task(gated(), gated(1), gated(2), APPROVE)

        self.assertEqual(fake.roles,
                         ["implement", "implement", "implement", "review"])
        self.assertEqual(len(self.summaries("witness_turn")), 2)
        goal = self.review_goal(fake)
        self.assertIn("witness pre-check found these gaps", goal)
        self.assertIn(GAP, goal)
        self.assert_merged_after_round_one()

    def test_a_resumed_run_takes_only_the_turns_its_paused_run_left(self):
        self.run_task(gated(), PausingCommit(self.db, gated(1)))
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("paused",)])

        command(self.project, "KO-131", None, resume=True)
        fake = self.run_task(gated(2), APPROVE)

        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertEqual(len(self.summaries("witness_turn")), 2)
        self.assertIn(GAP, self.review_goal(fake))

    def test_a_witness_turn_without_a_commit_ends_the_turns(self):
        fake = self.run_task(gated(), Idle(), APPROVE)

        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        self.assertEqual(self.summaries("witness_turn_idle"), [
            "witness turn 1 made no commit; review goes ahead with 1 gaps"])
        self.assertIn(GAP, self.review_goal(fake))

    def test_a_criterion_naming_no_test_file_starts_no_turn(self):
        fake = self.run_task(Commit("work"), APPROVE,
                             criteria=a_task()["criteria"])

        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertIn("Criteria that name no test file: C1.",
                      self.review_goal(fake))
        self.assertEqual(self.summaries("witness_turn"), [])

    def test_a_file_whose_runner_is_unchecked_starts_no_turn(self):
        fake = self.run_task(
            Commit("work"), APPROVE, verify="echo ok",
            criteria=["A test in `tests/test_runs.py` witnesses it."])

        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertEqual(self.summaries("witness_turn"), [])
        self.assertIn("criterion C1 names `tests/test_runs.py`; this branch"
                      " neither adds nor changes it", self.review_goal(fake))

    def test_a_witness_turn_that_turns_verify_red_ends_the_turns(self):
        fake = self.run_task(
            gated(), Commit("break verify", path="tests/test_broken.py",
                            body=BROKEN), APPROVE,
            Commit("mend verify", path="tests/test_broken.py", body=RUNS),
            BOTH_MET, criteria=(CRITERION, a_task()["criteria"][0]))

        self.assertEqual(fake.roles[:3], ["implement", "implement", "review"])
        self.assertEqual(len(self.summaries("witness_turn")), 1)
        goal = self.review_goal(fake)
        self.assertIn("FAILED with output below", goal)
        self.assertIn("broken by the witness turn", goal)
        (gapped,) = [sha for (sha,) in self.read(
            "SELECT payload->>'sha' FROM runEvents"
            " WHERE kind = 'witness_precheck_results'")]
        self.assertIn(f"found these gaps at {gapped[:12]}, before verify went"
                      f" red; judge each criterion yourself:\n{GAP}\n", goal)
        self.assertIn("Criteria that name no test file: C2.", goal)

    def test_a_red_verify_before_any_check_still_names_unnamed_criteria(self):
        fake = self.run_task(
            Commit("break verify", path="tests/test_broken.py", body=BROKEN),
            APPROVE, Commit("mend verify", path="tests/test_broken.py",
                            body=RUNS), APPROVE,
            criteria=a_task()["criteria"])

        self.assertEqual(fake.roles[:2], ["implement", "review"])
        self.assertEqual(self.summaries("witness_turn"), [])
        goal = self.review_goal(fake)
        self.assertIn("FAILED with output below", goal)
        self.assertIn("witness pre-check did not run, as verify failed; judge"
                      " each criterion yourself.\nCriteria that name no test"
                      " file: C1.\n", goal)

    def test_a_spent_time_box_fails_the_run_before_a_witness_turn(self):
        fake = FakeAgent(gated())
        real = holophyte.loop.implement.agent_work

        def spent(run, now):
            return 10 ** 10 if fake.turns else real(run, now)

        with patch.object(holophyte.loop.implement, "agent_work", spent):
            self.run_task(fake=fake)

        self.assertEqual(fake.roles, ["implement"])
        self.assertIn(GAP, self.summaries("witness_precheck")[0])
        self.assertEqual(self.summaries("witness_turn"), [])
        (cap,) = self.summaries("run_cap")
        self.assertTrue(cap.startswith("out of time:"), cap)
        self.assertEqual(self.read("SELECT outcomeReason, failureKind FROM runs"),
                         [(cap, "budget")])


if __name__ == "__main__":
    unittest.main()
