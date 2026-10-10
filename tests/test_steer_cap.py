"""A run that fails on its time or review-round cap while its ticket holds a
`holo steer` amendment parks the ticket on the operator with a split
suggestion; `holo steer KEY --withdraw` drops the amendments for the requeue.

Run: python3 -m unittest discover -s tests -p 'test_steer_cap.py' -v
"""
from __future__ import annotations

import json
from unittest.mock import patch

import holophyte.loop.review_round
import store
from tests.failure_triage_fixture import REQUEUE, FakeClaude, question_guard
from tests.fake_agent import APPROVE, FAIL, REQUEST_CHANGES, Commit
from tests.loop_fixture import StubProvider, a_task
from tests.test_steer import SteerFixture

NOTE = "also log the port\nand say which one"
TIME_CAP = "[supervisor]\nrun_cap = 3\n"
CAPPED_TASK = dict(a_task(), budget_min=30)
OUT_OF_TIME = (Commit("work", path="app.txt"), REQUEST_CHANGES)
ROUND_CAP = (Commit("work", path="app.txt"), REQUEST_CHANGES,
             Commit("fix", path="app.txt", body="fixed\n"), REQUEST_CHANGES,
             Commit("fix again", path="app.txt", body="fixed again\n"), FAIL)


class SteerCapFixture(SteerFixture):
    def aged(self, minutes=70):
        """Spend `minutes` of agent work when the first review starts, so the
        fix turn that follows no longer fits under `run_cap`."""
        real = holophyte.loop.review_round.set_phase
        done = []

        def watching(conn, run_id, phase, note=None):
            if phase == "reviewing" and not done:
                done.append(True)
                with store.transaction(conn):
                    conn.execute("UPDATE runs SET workingMs = workingMs + ?"
                                 " WHERE id = ?", (minutes * 60_000, run_id))
            return real(conn, run_id, phase, note)

        return patch.object(holophyte.loop.review_round, "set_phase", watching)

    def run_out_of_time(self, guard=None):
        with self.aged():
            return self.loop(*OUT_OF_TIME, provider=StubProvider(CAPPED_TASK),
                             guard=guard)

    def ticket_row(self):
        ((status, question),) = self.read(
            f"SELECT status, blockedQuestion FROM tickets WHERE id = {self.ticket}")
        return status, question

    def last_run(self):
        ((outcome, kind, reason),) = self.read(
            "SELECT outcome, failureKind, outcomeReason FROM runs"
            " ORDER BY id DESC LIMIT 1")
        return outcome, kind, reason

    def parks(self):
        return self.read("SELECT summary FROM runEvents"
                         " WHERE kind = 'steer_cap_park'")


class CapParkTests(SteerCapFixture):
    def test_a_steered_run_out_of_time_parks_naming_the_cap_and_the_way_out(self):
        self.configure(TIME_CAP)
        self.steer("-n", NOTE)

        self.run_out_of_time()

        outcome, kind, reason = self.last_run()
        self.assertEqual((outcome, kind), ("failed", "budget"))
        self.assertTrue(reason.startswith("out of time:"), reason)
        status, question = self.ticket_row()
        self.assertEqual(status, "blocked_on_operator")
        self.assertIn("KO-131 hit its time cap after steering added"
                      " 1 amendment(s): also log the port.", question)
        self.assertNotIn("which one", question)
        self.assertIn("holo steer KO-131 --withdraw", question)
        self.assertIn("holo requeue KO-131", question)
        self.assertEqual(self.parks(), [(question,)])

    def test_a_steered_run_failing_its_round_cap_adjudication_parks(self):
        self.steer("-n", NOTE)

        self.loop(*ROUND_CAP)

        outcome, _, reason = self.last_run()
        self.assertEqual(outcome, "failed")
        self.assertTrue(reason.startswith("terminal adjudication: FAIL"), reason)
        status, question = self.ticket_row()
        self.assertEqual(status, "blocked_on_operator")
        self.assertIn("KO-131 hit its review-round cap after steering added"
                      " 1 amendment(s): also log the port.", question)
        self.assertIn("holo steer KO-131 --withdraw", question)
        self.assertIn("holo requeue KO-131", question)

    def test_failure_triage_requeues_no_parked_steered_ticket(self):
        self.configure(REQUEUE + TIME_CAP)
        FakeClaude(self).answer("infra", 0.95)
        self.steer("-n", NOTE)

        self.run_out_of_time(guard=question_guard())

        self.assertEqual(self.ticket_row()[0], "blocked_on_operator")
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"
                                   " WHERE action = 'requeue'"), [(0,)])
        ((payload,),) = self.read("SELECT payload FROM runEvents"
                                  " WHERE kind = 'failure_triage'")
        self.assertEqual(json.loads(payload)["why"], "ticket not in flight")

    def assert_closed_out_as_before(self):
        self.run_out_of_time()

        self.assertEqual(self.last_run()[:2], ("failed", "budget"))
        self.assertEqual(self.ticket_row(), ("in_flight", None))
        self.assertEqual(self.parks(), [])

    def test_a_cap_on_an_unsteered_ticket_closes_out_as_before(self):
        self.configure(TIME_CAP)
        self.assert_closed_out_as_before()

    def test_a_cap_with_only_withdrawn_amendments_closes_out_as_before(self):
        self.configure(TIME_CAP)
        self.steer("-n", NOTE)
        self.steer("--withdraw", "-n", "split into a follow-up")
        self.assert_closed_out_as_before()

    def test_a_steered_run_failing_verify_closes_out_as_before(self):
        self.configure("[verify]\ntimeout_sec = 1\n")
        self.steer("-n", NOTE)
        failing = dict(a_task(), verify="echo started && sleep 5")

        self.loop(Commit("work", path="app.txt"), APPROVE,
                  provider=StubProvider(failing))

        outcome, kind, _ = self.last_run()
        self.assertEqual((outcome, kind), ("failed", "verify"))
        self.assertEqual(self.ticket_row(), ("in_flight", None))
        self.assertEqual(self.parks(), [])


class WithdrawTests(SteerCapFixture):
    def test_withdrawn_amendments_reach_no_prompt_after_the_requeue(self):
        self.configure(TIME_CAP)
        self.steer("-n", NOTE)
        self.run_out_of_time()
        self.assertEqual(self.ticket_row()[0], "blocked_on_operator")

        said = self.steer("--withdraw", "-n", "split into a follow-up")

        self.assertIn("1 amendment(s) withdrawn", said)
        ((intervention, note),) = self.read(
            "SELECT id, note FROM interventions WHERE action = 'steer'"
            " AND note LIKE 'withdraw%'")
        self.assertIn("split into a follow-up", note)
        self.assertEqual(self.read("SELECT withdrawnBy FROM steerNotes"),
                         [(intervention,)])
        store.requeue(self.conn, self.ticket, "the original scope")
        fake, _ = self.loop(Commit("the thing", path="app.txt"), APPROVE)
        prompts = self.prompts(fake)
        self.assertTrue(prompts["implement"] and prompts["review"])
        for goal in prompts["implement"] + prompts["review"]:
            self.assertNotIn("also log the port", goal)
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("failed",), ("merged",)])

    def test_a_ticket_with_a_live_run_is_refused_naming_it(self):
        self.steer("-n", NOTE)
        run = self.claim()
        before = list(self.conn.iterdump())

        code, said = self.holo("steer", "KO-131", "--withdraw", "-n",
                               "split into a follow-up")

        self.assertNotEqual(code, 0, said)
        self.assertIn(f"live run {run}", said)
        self.assertEqual(list(self.conn.iterdump()), before)
