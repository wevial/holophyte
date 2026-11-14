"""A gap the witness pre-check finds before round 1 goes back to the
implementer, up to two turns a run, before the review sees what is left;
a fix turn's diff that fails its self-check gets one more turn before the
next round's review.

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
    IMPLEMENT,
    PASS,
    REQUEST_CHANGES,
    REVIEW_ROLES,
    Commit,
    FakeAgent,
    Idle,
    Reply,
    _git,
)
from loop_fixture import LoopFixture, StubProvider, a_task  # noqa: E402 - as above

import holophyte.loop.implement  # noqa: E402 - as above
import store  # noqa: E402 - as above
from holophyte.loop.stop import command  # noqa: E402 - as above
from tests.test_main_baseline import Writes  # noqa: E402 - as above

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

import gate


class Gate(unittest.TestCase):
    @unittest.skip("needs a browser")
    def test_it_gates(self):
        pass
{extra}"""
OPENED = """import unittest

import gate


class Gate(unittest.TestCase):
    def test_it_gates(self):
        self.assertTrue(True)
"""
UNGATED = OPENED.replace("import gate\n\n", "")
BROKEN = """import unittest


class Broken(unittest.TestCase):
    def test_it_breaks(self):
        self.fail("broken by the witness turn")
"""

FIX_BROKE = BROKEN.replace("broken by the witness turn", "broken by the fix")
CITED = "fix\n\nADDRESS: lib/thing.py:3 the finding\n"
COUNTED = """import unittest

import {name}


class Counted(unittest.TestCase):
    def test_it_runs(self):
        with open({log!r}, "a") as log:
            log.write("{name}\\n")
"""
LIB = {"lib/thing.py": "THING = 1\n", "lib/other.py": "OTHER = 1\n"}
BOTH_MET = Reply("CRITERION 1: met \u2014 tests/test_gate.py::test_it_gates\n"
                 "CRITERION 2: met \u2014 tests/test_runs.py::test_it_runs\n"
                 "VERDICT: APPROVE")


def gated(n=0):
    return Writes({"gate.py": "OPEN = False\n", "tests/test_gate.py":
                   GATED.format(extra=f"# revision {n}\n" if n else "")})


class Declaring(Writes):
    """Writes the files and declares the reported defect not reproduced."""

    def play(self, cwd, turn):
        return f"{super().play(cwd, turn)}\nOUTCOME: NOT_REPRODUCED"


class PausingCommit:

    role = IMPLEMENT

    def __init__(self, db, step):
        self.db, self.step = db, step

    def play(self, cwd, turn):
        reply = self.step.play(cwd, turn)
        conn = store.open(self.db)
        try:
            ((run,),) = conn.execute(
                "SELECT id FROM runs WHERE endedAt IS NULL").fetchall()
            store.pause(conn, run, "reboot writer")
        finally:
            conn.close()
        return reply


class StaleApproval:
    """A review that cites an approval at the first commit of the gate test,
    which the witness turns have changed since."""

    role = REVIEW_ROLES

    def __init__(self, repo):
        self.repo = repo

    def play(self, cwd, turn):
        first = _git(self.repo, "log", "--all", "--reverse", "--format=%H",
                     "--", "tests/test_gate.py").split()[0]
        return (f"CRITERION 1: met \u2014 approval at {first[:12]}; "
                "tests/test_gate.py::test_it_gates\nVERDICT: APPROVE")


class RunningRepo(LoopFixture):

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


class WitnessTurns(RunningRepo):

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

    def test_a_fix_that_makes_the_test_pass_at_the_base_gets_a_turn(self):
        fake = self.run_task(
            Writes({"gate.py": "OPEN = True\n", "tests/test_gate.py": OPENED}),
            REQUEST_CHANGES,
            Commit("ungate the test", path="tests/test_gate.py", body=UNGATED),
            Commit("gate the test", path="tests/test_gate.py", body=OPENED),
            APPROVE)

        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "implement", "review"])
        fix = self.git("log", "main", "--format=%H", "--grep",
                       "ungate the test").strip()
        self.assertTrue(any(summary.startswith(f"witness pre-check at "
                                               f"{fix[:12]}:")
                            for summary in self.summaries("witness_precheck")))
        gap = (f"criterion C1's named test passes at the merge base "
               f"{self.base[:12]} without the change")
        (turn,) = self.summaries("witness_turn")
        self.assertIn(f"at {fix[:12]}: 1 gaps\n{gap}", turn)
        self.assertIn(gap, fake.turns[3].goal)
        self.assertIn(("Witness pre-check: turn 1 of 2 before round 2",),
                      self.read("SELECT text FROM ledger WHERE kind = 'note'"))
        self.assertNotIn("witness pre-check found these gaps",
                         fake.turns[4].goal)

    def test_a_gap_left_after_both_turns_reaches_the_round_two_review(self):
        fake = self.run_task(gated(), gated(1), gated(2), REQUEST_CHANGES,
                             Commit("notes", path="NOTES.md"), APPROVE)

        self.assertEqual(fake.roles, ["implement", "implement", "implement",
                                      "review", "implement", "review"])
        self.assertEqual(len(self.summaries("witness_turn")), 2)
        goal = fake.turns[5].goal
        self.assertIn(f"witness pre-check found these gaps before this review;"
                      f" judge each criterion yourself:\n{GAP}\n", goal)

    def test_a_stale_approval_rereview_keeps_the_open_gap(self):
        fake = self.run_task(gated(), gated(1), gated(2),
                             StaleApproval(self.target), APPROVE)

        self.assertEqual(fake.roles, ["implement", "implement", "implement",
                                      "review", "review"])
        self.assertEqual(len(self.summaries("stale_approval_rereview")), 1)
        self.assertIn(f"witness pre-check found these gaps before this review;"
                      f" judge each criterion yourself:\n{GAP}\n",
                      fake.turns[4].goal)

    def test_a_twice_refused_not_reproduced_round_two_gets_a_witness_turn(self):
        refused = Reply("The test never opens the gate.\nVERDICT: FAIL")
        ungated = {"gate.py": "OPEN = True\n", "tests/test_gate.py": UNGATED}
        fake = self.run_task(
            Declaring(ungated), refused,
            Declaring(dict(ungated, **{"gate.py": "OPEN = 1\n"})), refused,
            Commit("gate the test", path="tests/test_gate.py", body=OPENED),
            APPROVE)

        self.assertEqual(fake.roles, ["implement", "adjudicate", "implement",
                                      "adjudicate", "implement", "review"])
        gap = (f"criterion C1's named test passes at the merge base "
               f"{self.base[:12]} without the change")
        (turn,) = self.summaries("witness_turn")
        self.assertIn(gap, turn)
        self.assertIn(gap, fake.turns[4].goal)
        self.assertIn(("Witness pre-check: turn 1 of 2 before round 2",),
                      self.read("SELECT text FROM ledger WHERE kind = 'note'"))
        self.assertNotIn("witness pre-check found these gaps",
                         fake.turns[5].goal)

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
            Commit("run the gated test", path="tests/test_gate.py",
                   body=OPENED),
            BOTH_MET, criteria=(CRITERION, a_task()["criteria"][0]))

        self.assertEqual(fake.roles, ["implement", "implement", "review",
                                      "implement", "implement", "review"])
        goal = self.review_goal(fake)
        self.assertIn("FAILED with output below", goal)
        self.assertIn("broken by the witness turn", goal)
        gapped = self.read(
            "SELECT payload->>'sha' FROM runEvents"
            " WHERE kind = 'witness_precheck_results' ORDER BY seq")[0][0]
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


class FixSelfCheck(RunningRepo):

    def run_fix(self, fix, *after, verify=VERIFY):
        return self.run_task(Writes(LIB), REQUEST_CHANGES, fix, *after,
                             criteria=a_task()["criteria"], verify=verify)

    def head(self, grep):
        return self.git("log", "main", "--format=%H", "--grep", grep).strip()

    def test_a_fix_that_turns_verify_red_gets_one_more_turn(self):
        fake = self.run_fix(
            Commit("break", path="tests/test_runs.py", body=FIX_BROKE),
            Commit("mend", path="tests/test_runs.py", body=RUNS), APPROVE)

        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "implement", "review"])
        self.assertIn("verify failed:", fake.turns[3].goal)
        self.assertIn("broken by the fix", fake.turns[3].goal)
        mend = self.head("mend")
        self.assertIn(f"Review commit {mend}", fake.turns[4].goal)
        (event,) = self.summaries("fix_self_check")
        self.assertTrue(event.startswith(
            f"fix self-check at {self.head('break')[:12]}: 1 failures; the"
            f" extra turn committed, head {mend[:12]}\n"), event)

    def test_a_changed_test_verify_skips_is_run_and_named(self):
        fake = self.run_fix(
            Commit("break", path="tests/test_other.py", body=FIX_BROKE),
            Commit("mend", path="tests/test_other.py", body=RUNS), APPROVE,
            verify="python3 -m unittest discover -s tests -p 'test_runs.py'")

        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "implement", "review"])
        self.assertNotIn("verify failed:", fake.turns[3].goal)
        self.assertIn("tests/test_other.py is changed or imports a changed"
                      " module and failed", fake.turns[3].goal)
        self.assertIn("broken by the fix", fake.turns[3].goal)

    def test_an_address_citing_a_file_the_fix_leaves_gets_a_turn(self):
        fake = self.run_fix(Commit(CITED, path="NOTES.md"), Idle(), APPROVE)

        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "implement", "review"])
        self.assertIn("`ADDRESS: lib/thing.py:3 the finding` cites"
                      " `lib/thing.py`, but the fix changes no file in its"
                      " directory", fake.turns[3].goal)
        (event,) = self.summaries("fix_self_check")
        self.assertIn("1 failures; the extra turn made no commit", event)

    def test_a_fix_passing_every_check_takes_no_extra_turn(self):
        fake = self.run_fix(
            Commit(CITED, path="lib/other.py", body="OTHER = 2\n"), APPROVE)

        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "review"])
        self.assertEqual(self.summaries("fix_self_check"), [
            f"fix self-check passed at {self.head('ADDRESS')[:12]}"])

    def test_a_still_failing_extra_turn_takes_no_second_turn(self):
        fake = self.run_fix(
            Commit("break", path="tests/test_runs.py", body=FIX_BROKE),
            Commit("still", path="NOTES.md"), APPROVE,
            Commit("mend", path="tests/test_runs.py", body=RUNS), PASS)

        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "implement", "review", "implement",
                                      "adjudicate"])
        self.assertIn("FAILED with output below", fake.turns[4].goal)
        still = self.head("still")
        self.assertIn(f"Review commit {still}", fake.turns[4].goal)
        first, _ = self.summaries("fix_self_check")
        self.assertTrue(first.startswith(
            f"fix self-check at {self.head('break')[:12]}: 1 failures; the"
            f" extra turn committed, head {still[:12]}\nverify failed:"),
            first)
        self.assertIn("broken by the fix", first)

    def test_a_test_verify_ran_as_an_importer_is_not_run_again(self):
        log = str(self.target.parent / "ran.log")
        counted = {f"tests/test_{name}.py": COUNTED.format(name=name, log=log)
                   for name in ("alpha", "beta")}
        self.run_task(
            Writes({"alpha.py": "A = 1\n", "beta.py": "B = 1\n", **counted}),
            REQUEST_CHANGES, Commit("fix", path="alpha.py", body="A = 2\n"),
            APPROVE, criteria=a_task()["criteria"],
            verify="python3 -m unittest discover -s tests -p 'test_runs.py'")

        ran = Path(log).read_text().split()
        self.assertEqual(ran.count("alpha"), ran.count("beta"), ran)

    def test_a_spent_time_box_still_records_the_self_check(self):
        fake = FakeAgent(Writes(LIB), REQUEST_CHANGES,
                         Commit("break", path="tests/test_runs.py",
                                body=FIX_BROKE))
        real = holophyte.loop.implement.agent_work

        def spent(run, now):
            return 10 ** 10 if len(fake.turns) == 3 else real(run, now)

        with patch.object(holophyte.loop.implement, "agent_work", spent):
            self.run_task(fake=fake, criteria=a_task()["criteria"])

        self.assertEqual(len(fake.turns), 3)
        (event,) = self.summaries("fix_self_check")
        self.assertIn("1 failures; the extra turn never started", event)
        self.assertIn("broken by the fix", event)
        self.assertEqual(len(self.summaries("run_cap")), 1)


if __name__ == "__main__":
    unittest.main()
