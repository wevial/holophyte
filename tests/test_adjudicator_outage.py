from __future__ import annotations

import io
import sys
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name

from fake_agent import (  # noqa: E402
    APPROVE,
    REQUEST_CHANGES,
    REVIEW_ROLES,
    Commit,
    Idle,
    Reply,
)
from loop_fixture import LoopFixture, MergeModeFixture, StubProvider  # noqa: E402

import holophyte.host.reconcile  # noqa: E402
import store  # noqa: E402
from holophyte.agents.agent_output import AgentOutput  # noqa: E402

LIMIT = "You've hit your session limit · resets 3:20am (UTC)"


@dataclass(frozen=True)
class AdjudicatorTurn:
    """An adjudicator turn whose command printed `text` and exited `exit_code`."""

    text: str
    exit_code: int = 1
    timed_out: bool = False

    role = REVIEW_ROLES

    def play(self, cwd, turn):
        return AgentOutput(self.text + "\n" if self.text else "", "claude -p",
                           exit_code=self.exit_code, timed_out=self.timed_out)


class AdjudicatorOutageTests(MergeModeFixture):
    def test_a_failed_adjudicator_turn_parks_as_a_route_outage(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT])])

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            AdjudicatorTurn(LIMIT), provider=self.provider())

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

    def test_an_empty_timed_out_turn_parks_naming_the_timeout(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT])])

        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  AdjudicatorTurn("", timed_out=True), provider=self.provider())

        self.assertEqual(self.read("SELECT parkKind FROM runs"), [("route",)])
        question = self.question()
        self.assertIn("the adjudicator turn failed: timed out", question)
        self.assertNotIn("exit status", question)

    def test_a_reply_judging_neither_thread_parks_as_a_route_outage(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT, self.NIT])])

        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  AdjudicatorTurn("I read both threads.", exit_code=0),
                  provider=self.provider())

        self.assertEqual(self.read("SELECT parkKind FROM runs"), [("route",)])
        self.assertIn("no verdict for any thread", self.question())

    def test_a_verdict_for_thread_one_only_parks_thread_two_as_human(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT, self.NIT])])

        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  Reply("THREAD 1: DECLINE -- a naming preference"),
                  provider=self.provider())

        self.assertEqual(self.read("SELECT parkKind FROM runs"), [("thread",)])
        question = self.question()
        self.assertIn("a thread needs a human's answer", question)
        self.assertIn("thing_count", question)
        self.assertNotIn("the adjudicator turn failed", question)


PARKED_S = 200
RETRY_S = 900


class RouteParkWakeTests(MergeModeFixture):
    def route_parked(self, retry_s=RETRY_S, aged_s=PARKED_S):
        self.configure(f'[merge]\nmode = "pr"\nroute_retry_sec = {retry_s}\n')
        self.fake_route(states=[self.pr_state([self.DEFECT])])
        with patch.object(sys, "stdout", io.StringIO()):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      AdjudicatorTurn(LIMIT), provider=self.provider())
        self.assertEqual(self.read("SELECT parkKind FROM runs"), [("route",)])
        conn = store.open(self.db)
        with conn:
            conn.execute("UPDATE runs SET lastHeartbeat = lastHeartbeat - ?",
                         (aged_s * 1000,))
        conn.close()

    def reconcile(self, later_s):
        from test_pullrequest_parked import ParkedPullRequestTests as H
        H.fake_client(self, H.open_pull(H, "2000-01-01T00:00:00Z", 1,
                                        checks="SUCCESS"))
        conn = store.open(self.db)
        self.addCleanup(conn.close)
        with patch("holophyte.host.ci_wake.time",
                   return_value=time.time() + later_s), \
                patch.object(sys, "stdout", io.StringIO()):
            holophyte.host.reconcile._reconcile_pull_requests(
                self.project, conn,
                conn.execute("SELECT id FROM projects").fetchone()[0],
                StubProvider())

    def wakes(self):
        return self.read("SELECT e.summary FROM runEvents e JOIN interventions i"
                         " ON i.runId = e.runId AND i.action = 'babysit'"
                         " WHERE e.kind = 'ci_wake'")

    def test_a_route_park_wakes_once_route_retry_sec_has_passed(self):
        self.route_parked()

        self.reconcile(later_s=RETRY_S - PARKED_S - 1)
        self.assertEqual(self.wakes(), [])
        self.assertEqual(self.read("SELECT phase, parkKind FROM runs"),
                         [("awaiting_merge_approval", "route")])

        self.reconcile(later_s=RETRY_S - PARKED_S)
        self.assertEqual(self.wakes(),
                         [("retrying the adjudicator after a route outage",)])

    def test_a_route_retry_shorter_than_pr_poll_sec_wakes_when_due(self):
        self.route_parked(retry_s=1, aged_s=0)

        self.reconcile(later_s=2)
        self.assertEqual(self.wakes(),
                         [("retrying the adjudicator after a route outage",)])

    def test_an_older_redirect_does_not_shorten_the_route_retry(self):
        self.route_parked()
        conn = store.open(self.db)
        with conn:
            (run_id, parked_ms), = conn.execute(
                "SELECT id, lastHeartbeat FROM runs").fetchall()
            store.record_intervention(conn, run_id, "redirect", "try again",
                                      question="which route?",
                                      now=parked_ms - 2 * RETRY_S * 1000)
        conn.close()

        self.reconcile(later_s=RETRY_S - PARKED_S - 1)
        self.assertEqual(self.wakes(), [])


class TerminalAdjudicationOutageTests(LoopFixture):
    def terminal(self, turn):
        self.loop(Commit("first cut"), REQUEST_CHANGES,
                  Commit("fix round 1"), REQUEST_CHANGES,
                  Commit("fix round 2"), turn)
        return self.read("SELECT outcome, failureKind, outcomeReason FROM runs")

    def test_a_failed_terminal_turn_fails_the_run_as_infra(self):
        ((outcome, kind, reason),) = self.terminal(AdjudicatorTurn(LIMIT))

        self.assertEqual((outcome, kind), ("failed", "infra"))
        self.assertIn(LIMIT, reason)

    def test_a_terminal_turn_exiting_0_without_a_verdict_stays_malformed(self):
        ((outcome, kind, reason),) = self.terminal(
            AdjudicatorTurn("I have thoughts but no verdict.", exit_code=0))

        self.assertEqual((outcome, kind), ("failed", "review_route"))
        self.assertIn("MALFORMED", reason)


if __name__ == "__main__":
    unittest.main()
