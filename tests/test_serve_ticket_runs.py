"""`/tickets/KEY` lists every run of the ticket and every steer note on it,
newest first."""
from __future__ import annotations

import sys
from pathlib import Path
from time import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serve_fixture import MIN, SEC, ServeTestCase  # noqa: E402 - after the insert

import store  # noqa: E402 - after the sys.path insert above
import store.steer_notes  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from tests.phase_fixture import finish_run  # noqa: E402

PR_URL = "https://example.test/pull/3"


class TicketRunsTests(ServeTestCase):
    maxDiff = None

    def setUp(self):
        super().setUp()
        self.now = int(time() * 1000)
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.tickets.ensure_project(self.conn, "team-1",
                                                       self.target)

    def ticket(self, key):
        return store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{key}",
            linear_identifier=key, title=f"ticket {key}",
            acceptance_criteria=[f"Given {key}, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=60 * MIN)

    def claim(self, ticket, at, queued=True):
        if queued:
            store.tickets.transition(self.conn, ticket, "in_flight")
        run = store.claim(self.conn, self.project_id, ticket, now=at)
        store.set_phase(self.conn, run, "working", now=at)
        return run

    def review(self, run, *rounds):
        for number, at in enumerate(rounds, 1):
            store.record_review_round(self.conn, run, number,
                                      "changes_requested", "reviewer",
                                      started_at=at, ended_at=at + MIN)

    def answer(self, key):
        self.conn.commit()
        self.start()
        code, _, body = self.request("GET", f"/tickets/{key}")
        self.assertEqual(code, 200, body)
        return body

    def two_attempts(self):
        """DEMO-3: an amendment before any run, a failed first attempt with
        one round, a second in review with two rounds that read a hint."""
        ticket = self.ticket("DEMO-3")
        amendment = store.steer_notes.steer(
            self.conn, ticket, "cover the empty case", "maintainer",
            now=self.now - 40 * MIN)
        first = self.claim(ticket, self.now - 30 * MIN)
        self.review(first, self.now - 25 * MIN)
        finish_run(self.conn, first, "failed", "verify failed",
                   now=self.now - 20 * MIN)
        store.requeue(self.conn, ticket, "rerun it")
        second = self.claim(ticket, self.now - 15 * MIN)
        hint = store.steer_notes.steer(
            self.conn, ticket, "reuse the lease query", "maintainer",
            hint=True, now=self.now - 12 * MIN)
        read_at = self.now - 11 * MIN + 7 * SEC
        store.steer_notes.consume(self.conn, [hint.id], second, now=read_at)
        self.review(second, self.now - 10 * MIN, self.now - 6 * MIN)
        for phase in ("verifying", "reviewing"):
            store.set_phase(self.conn, second, phase)
        store.set_pull_request(self.conn, second, PR_URL)
        return first, second, amendment, hint, read_at

    def test_runs_newest_attempt_first_with_rounds_outcome_and_pull_request(self):
        first, second, *_ = self.two_attempts()

        runs = self.answer("DEMO-3")["runs"]

        self.assertEqual(
            [{k: r[k] for k in ("run", "attempt", "phase", "outcome", "rounds",
                                "pr_url", "ended_ms")} for r in runs],
            [{"run": second, "attempt": 2, "phase": "reviewing",
              "outcome": None, "rounds": 2, "pr_url": PR_URL,
              "ended_ms": None},
             {"run": first, "attempt": 1, "phase": "failed",
              "outcome": "failed", "rounds": 1, "pr_url": None,
              "ended_ms": self.now - 20 * MIN}])
        self.assertEqual([r["started_ms"] for r in runs],
                         [self.now - 15 * MIN, self.now - 30 * MIN])

    def test_steers_are_the_tickets_own_newest_first_with_reader(self):
        _, second, amendment, hint, read_at = self.two_attempts()
        store.steer_notes.steer(self.conn, self.ticket("DEMO-4"),
                                "another ticket's note", "maintainer",
                                now=self.now - MIN)

        steers = self.answer("DEMO-3")["steers"]

        self.assertEqual(steers, [
            {"id": hint.id, "kind": "hint", "note": "reuse the lease query",
             "author": "maintainer", "at": self.now - 12 * MIN,
             "run": second, "read_by": second, "read_ms": read_at,
             "withdrawn": False},
            {"id": amendment.id, "kind": "amendment",
             "note": "cover the empty case", "author": "maintainer",
             "at": self.now - 40 * MIN, "run": None, "read_by": None,
             "read_ms": None, "withdrawn": False}])

    def test_a_ticket_never_claimed_nor_steered_has_no_runs_or_steers(self):
        self.ticket("DEMO-5")

        body = self.answer("DEMO-5")

        self.assertEqual((body["runs"], body["steers"]), ([], []))
