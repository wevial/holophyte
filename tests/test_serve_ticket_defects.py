"""`/findings` lists ticket defects: runs that stopped on a criterion, by a
contradiction or a criterion or review sweep, whose ticket was then revised
before any later claim.

The stops are written by `contradiction.park()` on a real repository and by
an acting supervisor sweep; revisions, requeues and claims go through the
store's own writers, and the answer is read from a real daemon.

Run: python3 -m unittest discover -s tests -p 'test_serve_ticket_defects.py'
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from serve_fixture import ServeTestCase  # noqa: E402 - after the insert
from sweep_fixture import (  # noqa: E402 - after the insert
    MINUTE,
    T0,
    StubProvider,
    SweepTestCase,
)
from test_review_stuck_criteria import code, restated  # noqa: E402

import holophyte.host.supervisor  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from holophyte.loop.contradiction import park  # noqa: E402
from holophyte.loop.gates import MergeParked  # noqa: E402

DECLARED = "CONTRADICTION: criterion C2 — the export has no header field"
NOTE = "C2 now names the export's columns"


class TicketDefectsCase(SweepTestCase, ServeTestCase):
    """The sweep fixture's store, served by the serve fixture's daemon."""

    def setUp(self):
        super().setUp()
        for args in (["init", "-q", "-b", "main"],
                     ["-c", "user.name=t", "-c", "user.email=t@example.test",
                      "commit", "-q", "--allow-empty", "-m", "base"]):
            subprocess.run(["git", *args], cwd=self.target, check=True)

    def contradicted(self):
        """Run 1 of a new ticket, parked on criterion C2."""
        run_id = self.a_run(budget_min=None)
        with self.assertRaises(MergeParked), redirect_stdout(io.StringIO()):
            park(self.conn, run_id, StubProvider(), f"KO-{self.tickets}",
                 str(self.target), 1, f"Criterion 1 is done.\n{DECLARED}",
                 False)
        return run_id

    def revise(self, run_id):
        """A body edit of the run's ticket; returns the time it is written at."""
        issue, identifier, title, criteria = self.conn.execute(
            "SELECT linearIssueId, linearIdentifier, title, acceptanceCriteria"
            " FROM tickets WHERE id = ?", (self.ticket_of[run_id],)).fetchone()
        at = self.clock_past(run_id)
        store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=issue,
            linear_identifier=identifier, title=title,
            acceptance_criteria=json.loads(criteria),
            verification_commands=["echo ok"], body=f"revised at {at}", now=at)
        return at

    def clock_past(self, run_id):
        """A wall-clock millisecond after every event the run recorded."""
        (newest,) = self.conn.execute(
            "SELECT max(at) FROM runEvents WHERE runId = ?",
            (run_id,)).fetchone()
        while (now := int(time.time() * 1000)) <= newest:
            time.sleep(0.001)
        return now

    def stopped_at(self, run_id):
        (at,) = self.conn.execute(
            "SELECT at FROM runEvents WHERE runId = ? AND kind = 'contradiction'",
            (run_id,)).fetchone()
        return at

    def claim_again(self, run_id):
        ticket = self.ticket_of[run_id]
        store.tickets.transition(self.conn, ticket, "in_flight")
        return self.a_run(ticket=ticket)

    def defects(self, since=0):
        self.start()
        code_, _, body = self.request("GET", f"/findings?since={since}")
        self.assertEqual(code_, 200)
        return body["ticket_defects"]

    def swept_in_review(self, *findings_by_round):
        run_id = self.a_run(phase="addressing")
        for number, findings in enumerate(findings_by_round, 1):
            at = T0 + number * 2 * MINUTE
            store.record_review_round(
                self.conn, run_id, number, "changes_requested", "reviewer",
                findings=findings, started_at=at, ended_at=at + MINUTE)
        self.heartbeat_at(run_id, T0 + 20 * MINUTE)
        holophyte.host.supervisor.sweep(self.project, self.conn,
                                        T0 + 20 * MINUTE, act=True)
        return run_id


class TicketDefectTests(TicketDefectsCase):

    def test_a_contradiction_revised_and_requeued_is_one_defect(self):
        first = self.contradicted()
        revised_at = self.revise(first)
        store.requeue(self.conn, self.ticket_of[first], NOTE)
        self.claim_again(first)

        (defect,) = self.defects()

        self.assertEqual(
            {key: defect[key] for key in ("ticket", "run", "kind", "criteria",
                                          "at", "revision", "revised_ms",
                                          "requeue_note")},
            {"ticket": "KO-1", "run": first, "kind": "contradiction",
             "criteria": ["C2"], "at": self.stopped_at(first), "revision": 2,
             "revised_ms": revised_at, "requeue_note": NOTE})
        self.assertIn("criterion C2 cannot be met as written: the export has"
                      " no header field", defect["reason"])

    def test_a_criterion_or_review_sweep_then_revised_names_its_kind(self):
        cases = {
            "criterion_stuck": ((code("a.py", 1), *restated(1, 2)),
                                (code("b.py", 2), *restated(2, 3)),
                                (code("c.py", 3), *restated(2)), ["C2"]),
            "review_stuck": ((code("a.py", 1),), (code("a.py", 1),), []),
        }
        for kind, (*rounds, criteria) in cases.items():
            with self.subTest(kind):
                run_id = self.swept_in_review(*(list(r) for r in rounds))
                self.revise(run_id)

                (defect,) = [d for d in self.defects() if d["run"] == run_id]

                self.assertEqual((defect["kind"], defect["criteria"]),
                                 (kind, criteria))
                self.assertIn(f"supervisor sweep: {kind}", defect["reason"])

    def test_two_defects_list_newest_stop_first_and_since_drops_the_older(self):
        older = self.contradicted()
        self.revise(older)
        newer = self.contradicted()
        self.revise(newer)

        older_at = self.stopped_at(older)
        self.assertLess(older_at, self.stopped_at(newer))

        self.assertEqual([d["run"] for d in self.defects()], [newer, older])
        self.assertEqual([d["run"] for d in self.defects(older_at + 1)],
                         [newer])


    def test_a_contradiction_recorded_before_criterion_ids_names_its_number(self):
        run_id = self.a_run(budget_min=None)
        store.record_event(
            self.conn, run_id, "contradiction",
            "implementer: criterion C3 cannot be met as written: no field",
            level="detail", payload=json.dumps({
                "turn": 1, "head": "abc",
                "criteria": [{"criterion": 3, "reason": "no field"}]}))
        self.revise(run_id)

        self.assertEqual([d["criteria"] for d in self.defects()], [["C3"]])


class NotADefectTests(TicketDefectsCase):

    def test_a_requeue_without_an_edit_or_a_time_box_sweep_is_no_defect(self):
        with self.subTest("requeued unedited, edited after the next claim"):
            first = self.contradicted()
            store.requeue(self.conn, self.ticket_of[first], NOTE)
            self.claim_again(first)
            self.revise(first)

            self.assertEqual(self.defects(), [])
        with self.subTest("failed by a time_box sweep, then revised"):
            run_id = self.a_run(active_work=True, budget_min=20)
            at = T0 + 31 * MINUTE
            self.heartbeat_at(run_id, at)
            (outcome,) = holophyte.host.supervisor.sweep(
                self.project, self.conn, at, act=True).outcomes
            self.assertEqual((outcome.trip.condition, outcome.acted),
                             ("time_box", True))
            self.revise(run_id)
            store.requeue(self.conn, self.ticket_of[run_id], NOTE)
            self.claim_again(run_id)

            self.assertEqual(self.defects(), [])


if __name__ == "__main__":
    unittest.main()
