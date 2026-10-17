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
    def seed_history(self, window=False):
        """Run 3 merged two days before `since`, run 4 failed at verify
        after it, run 5 parked on CI before it, run 6 reviewing now."""
        self.now = int(time() * 1000)
        self.since = self.now - (3 * DAY if window else 6 * 60 * MIN)
        self.until = self.since + DAY
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            plan = {3: self.since - 2 * DAY, 4: self.since + 60 * MIN,
                    5: self.since - 3 * 60 * MIN, 6: self.now - 20 * MIN}
            if window:
                plan.update({3: self.since - DAY, 5: self.since + 2 * 60 * MIN,
                             6: self.now, 7: self.until - 60 * MIN,
                             9: self.until})
            self.runs = {}
            for n, started in plan.items():
                ticket = store.tickets.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{n}",
                    linear_identifier=f"KO-{n}", title=f"ticket {n}",
                    acceptance_criteria=[f"Given {n}, then it is worked"],
                    verification_commands=["echo ok"],
                    time_box_ms=30 * MIN if n == 4 else None)
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
            if window:
                for n in (7, 9):
                    finish_run(conn, self.runs[n], "merged", now=plan[n] + MIN)
            with store.transaction(conn):
                conn.execute("UPDATE runs SET workingMs = ? WHERE id = ?",
                             (26 * MIN, self.runs[4]))
                conn.execute("UPDATE runs SET workingMs = NULL WHERE id = ?",
                             (self.runs[5],))
                if window:
                    conn.execute("UPDATE runs SET workingMs = NULL WHERE id = ?",
                                 (self.runs[7],))
                conn.execute("UPDATE runs SET workingMs = ?, workStartedAt = ?"
                             " WHERE id = ?", (MIN, self.now - 2 * MIN, self.runs[6]))
            store.tickets.mirror_ticket(
                conn, project, linear_issue_id="issue-4", linear_identifier="KO-4",
                title="ticket 4", time_box_ms=60 * MIN)
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

    def test_past_window_excludes_open_runs_and_counts_before_limit(self):
        self.seed_history(window=True)
        for limit, expected, truncated in ((200, [7, 4], False), (1, [7], True)):
            with self.subTest(limit=limit):
                code, _, body = self.request(
                    "GET", f"/history?since={self.since}&until={self.until}"
                    f"&limit={limit}")
                self.assertEqual(code, 200)
                self.assertEqual([row["id"] for row in body["rows"]],
                                 [self.runs[n] for n in expected])
                self.assertEqual((body["until"], body["total"], body["truncated"]),
                                 (self.until, 2, truncated))
        code, _, empty = self.request(
            "GET", f"/history?since={self.since - 3 * DAY}"
            f"&until={self.since - 2 * DAY}")
        self.assertEqual(code, 200)
        self.assertEqual((empty["rows"], empty["total"], empty["truncated"]),
                         ([], 0, False))

    def test_work_and_claimed_estimate_in_both_history_forms(self):
        self.seed_history(window=True)
        for suffix in ("", f"&until={self.until}"):
            with self.subTest(suffix=suffix):
                code, _, body = self.request(
                    "GET", f"/history?since={self.since}{suffix}")
                self.assertEqual(code, 200)
                rows = {row["id"]: row for row in body["rows"]}
                self.assertEqual((rows[self.runs[4]]["estimate_ms"],
                                  rows[self.runs[4]]["working_ms"]), (1800000, 1560000))
                self.assertIsNone(rows[self.runs[7]]["estimate_ms"])
                self.assertIsNone(rows[self.runs[7]]["working_ms"])
                if not suffix:
                    self.assertIsNone(body["until"])
                    self.assertEqual(body["total"], 5)
                    self.assertIsNone(rows[self.runs[5]]["working_ms"])
                    self.assertEqual(rows[self.runs[6]]["working_ms"],
                                     body["now"] - self.now + 3 * MIN)

    def test_until_rejects_non_integer_and_non_increasing_bounds(self):
        self.seed_history()
        for until in ("soon", str(self.since), str(self.since - 1)):
            with self.subTest(until=until):
                code, _, body = self.request(
                    "GET", f"/history?since={self.since}&until={until}")
                self.assertEqual(code, 400)
                if until == "soon":
                    self.assertIn("until", body["error"])
                else:
                    self.assertEqual(body["error"], "until must be after since")

    def test_a_missing_or_non_integer_since_is_400_naming_it(self):
        self.seed_history()
        for path in ("/history", "/history?since=soon"):
            with self.subTest(path=path):
                code, _, body = self.request("GET", path)
                self.assertEqual(code, 400)
                self.assertIn("since", body["error"])


if __name__ == "__main__":
    unittest.main()
