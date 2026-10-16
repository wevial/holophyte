"""`GET /history`: open runs and runs started since a time, one row per run.

Run: python3 -m unittest discover -s tests -p 'test_serve_history.py' -v
"""
import unittest
from time import time

import store
import store.tickets
from tests.phase_fixture import advance_phase, finish_run, park_run
from tests.serve_fixture import MIN, ServeTestCase

DAY = 24 * 60 * MIN


class HistoryTests(ServeTestCase):
    def seed_history(self):
        """Run 3 merged two days before `since`, run 4 failed at verify
        after it, run 5 parked on CI before it, run 6 reviewing now."""
        self.now = int(time() * 1000)
        self.since = self.now - 6 * 60 * MIN
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            plan = {3: self.since - 2 * DAY, 4: self.since + 60 * MIN,
                    5: self.since - 3 * 60 * MIN, 6: self.now - 20 * MIN}
            self.runs = {}
            for n, started in plan.items():
                ticket = store.tickets.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{n}",
                    linear_identifier=f"KO-{n}", title=f"ticket {n}",
                    acceptance_criteria=[f"Given {n}, then it is worked"],
                    verification_commands=["echo ok"], time_box_ms=None)
                store.tickets.transition(conn, ticket, "in_flight")
                self.runs[n] = store.claim(conn, project, ticket, now=started)
            finish_run(conn, self.runs[3], "merged", now=plan[3] + 30 * MIN)
            finish_run(conn, self.runs[4], "failed", "verify failed: ruff",
                       failure_kind="verify", now=plan[4] + 30 * MIN)
            park_run(conn, self.runs[5], "awaiting_merge_approval", "CI red",
                     park_kind="ci", now=plan[5] + 30 * MIN)
            advance_phase(conn, self.runs[6], "reviewing", now=self.now - 5 * MIN)
            store.record_review_round(conn, self.runs[6], 1, "changes_requested",
                                      "reviewer-model",
                                      started_at=self.now - 4 * MIN)
            store.heartbeat(conn, self.runs[6], now=self.now - MIN)
        finally:
            conn.close()
        self.start()

    def test_open_runs_and_runs_started_since_come_newest_start_first(self):
        self.seed_history()
        code, _, body = self.request("GET", f"/history?since={self.since}")
        self.assertEqual(code, 200)
        rows = {row["id"]: row for row in body["rows"]}
        self.assertEqual([row["id"] for row in body["rows"]],
                         [self.runs[6], self.runs[4], self.runs[5]])
        failed, parked, live = (rows[self.runs[n]] for n in (4, 5, 6))
        self.assertEqual((failed["outcome"], failed["failure_kind"]),
                         ("failed", "verify"))
        self.assertIsInstance(failed["ended_ms"], int)
        self.assertEqual((parked["phase"], parked["park_kind"], parked["outcome"]),
                         ("awaiting_merge_approval", "ci", None))
        self.assertEqual((live["phase"], live["rounds"], live["ended_ms"]),
                         ("reviewing", 1, None))
        self.assertGreaterEqual(live["heartbeat_age_ms"], MIN)
        self.assertIsNone(failed["heartbeat_age_ms"])
        self.assertEqual((body["since"], body["truncated"]), (self.since, False))

    def test_a_limit_cuts_the_list_and_says_so(self):
        self.seed_history()
        code, _, body = self.request("GET",
                                     f"/history?since={self.since}&limit=2")
        self.assertEqual(code, 200)
        self.assertEqual([row["id"] for row in body["rows"]],
                         [self.runs[6], self.runs[4]])
        self.assertEqual((body["limit"], body["truncated"]), (2, True))

    def test_a_missing_or_non_integer_since_is_400_naming_it(self):
        self.seed_history()
        for path in ("/history", "/history?since=soon"):
            with self.subTest(path=path):
                code, _, body = self.request("GET", path)
                self.assertEqual(code, 400)
                self.assertIn("since", body["error"])


if __name__ == "__main__":
    unittest.main()
