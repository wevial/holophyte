"""`GET /history`: open runs and runs started since a time, one row per run.

Run: python3 -m unittest discover -s tests -p 'test_serve_history.py' -v
"""
import unittest
from time import time

import store
import store.tickets
from holophyte.serve.contract import History
from tests.phase_fixture import advance_phase, finish_run, park_run
from tests.serve_fixture import MIN, ServeTestCase
from tests.test_serve_contract import walk

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

    def test_findings_merge_sha_and_pull_request_size_in_both_forms(self):
        self.seed_history(window=True)
        sha = "a" * 40
        finding = dict(path="app.py", line=1, severity="p2", message="fix it")
        conn = store.open(str(self.db))
        try:
            store.record_review_round(conn, self.runs[7], 1, "changes_requested",
                                      "reviewer", findings=[finding] * 2)
            store.record_review_round(conn, self.runs[7], 2, "pass",
                                      "reviewer", findings=[finding])
            store.record_review_round(conn, self.runs[4], 1, "changes_requested",
                                      "reviewer", findings=[finding])
            store.record_pr_seen(conn, self.runs[7], (self.now, 0, 0, None,
                                 "success", "APPROVED", "ticket 7", "MERGED",
                                 120, 14))
            with store.transaction(conn):
                conn.execute("UPDATE runs SET mergeSha = ? WHERE id = ?",
                             (sha, self.runs[7]))
        finally:
            conn.close()
        for suffix in ("", f"&until={self.until}"):
            with self.subTest(suffix=suffix):
                code, _, body = self.request(
                    "GET", f"/history?since={self.since}{suffix}")
                self.assertEqual(code, 200)
                rows = {row["id"]: row for row in body["rows"]}
                merged, failed = rows[self.runs[7]], rows[self.runs[4]]
                self.assertEqual(
                    (merged["findings"], merged["merge_sha"],
                     merged["additions"], merged["deletions"]),
                    (3, sha, 120, 14))
                self.assertEqual(
                    (failed["findings"], failed["merge_sha"],
                     failed["commit_url"], failed["additions"],
                     failed["deletions"]), (1, None, None, None, None))

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

    def test_a_paused_run_names_the_next_run_of_its_ticket_in_both_forms(self):
        from holophyte.loop.stop import stop_if_requested
        now = int(time() * 1000)
        since, until = now - 3 * 60 * MIN, now - 2 * 60 * MIN
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        store.init(conn)
        project = store.tickets.ensure_project(conn, "team-1", self.target)

        def claim(key, at, resumed=False):
            ticket = store.tickets.mirror_ticket(
                conn, project, linear_issue_id=f"issue-{key}",
                linear_identifier=key, title=f"ticket {key}",
                acceptance_criteria=[f"Given {key}, then it is worked"],
                verification_commands=["echo ok"])
            steps = ("blocked_on_deps", "ready") if resumed else ()
            for status in (*steps, "in_flight"):
                store.tickets.transition(conn, ticket, status)
            return ticket, store.claim(conn, project, ticket, now=at)

        def pause(run):
            store.pause(conn, run, "reboot the writer")
            with self.assertRaises(store.RunEnded):
                stop_if_requested(conn, run, "working")

        _, r1 = claim("KO-1", since + MIN)
        pause(r1)
        _, r3 = claim("KO-2", since + 2 * MIN)
        pause(r3)
        _, merged = claim("KO-3", since + 3 * MIN)
        finish_run(conn, merged, "merged", now=since + 4 * MIN)
        _, r2 = claim("KO-1", now - MIN, resumed=True)
        self.assertGreater(r2, r3)
        self.start()
        code, _, body = self.request("GET", f"/history?since={since}")
        self.assertEqual(code, 200)
        walk(body, History, "history")
        rows = {row["id"]: row for row in body["rows"]}
        self.assertEqual((rows[r1]["outcome"], rows[r3]["outcome"]),
                         ("paused", "paused"))
        self.assertEqual(
            {run: rows[run].get("resumed_as", "absent")
             for run in (r1, r3, r2, merged)},
            {r1: r2, r3: None, r2: None, merged: None})
        code, _, body = self.request(
            "GET", f"/history?since={since}&until={until}")
        self.assertEqual(code, 200)
        walk(body, History, "history")
        rows = {row["id"]: row for row in body["rows"]}
        self.assertNotIn(r2, rows)
        self.assertEqual(rows[r1].get("resumed_as", "absent"), r2)


class FlowTests(ServeTestCase):
    def seed_flow(self):
        """KO-1 merged in the older week after 30 minutes parked on pending
        checks; in the newer week KO-2 merged after 60 minutes parked on a
        thread, KO-3 merged with no stays, and KO-4 failed after 30 minutes
        blocked on the operator. Each week holds one approval, the newer one
        a second and the store's migration row too."""
        self.now = int(time() * 1000)
        older, newer = self.now - 10 * DAY, self.now - 3 * DAY
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            runs = {}
            for n, started in ((1, older), (2, newer), (3, self.now - DAY),
                               (4, self.now - 2 * DAY)):
                ticket = store.tickets.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{n}",
                    linear_identifier=f"KO-{n}", title=f"ticket {n}",
                    acceptance_criteria=[f"Given {n}, then it is worked"],
                    verification_commands=["echo ok"])
                store.tickets.transition(conn, ticket, "in_flight")
                runs[n] = store.claim(conn, project, ticket, now=started)
            for phase, at in (("working", older + 10 * MIN),
                              ("verifying", older + 70 * MIN),
                              ("reviewing", older + 80 * MIN),
                              ("merge_gate", older + 100 * MIN)):
                store.set_phase(conn, runs[1], phase, now=at)
            park_run(conn, runs[1], "awaiting_merge_approval",
                     "pending checks (unit) exceeded 1800s on the pull request",
                     now=older + 120 * MIN)
            finish_run(conn, runs[1], "merged", now=older + 150 * MIN)
            for phase, at in (("working", newer), ("reviewing", newer + 30 * MIN),
                              ("merge_gate", newer + 45 * MIN)):
                advance_phase(conn, runs[2], phase, now=at)
            park_run(conn, runs[2], "awaiting_merge_approval",
                     "a thread needs a human's answer: is the cap right?",
                     park_kind="thread", now=newer + 60 * MIN)
            finish_run(conn, runs[2], "merged", now=newer + 120 * MIN)
            finish_run(conn, runs[3], "merged", now=self.now - DAY)
            store.set_phase(conn, runs[4], "working", now=self.now - 2 * DAY)
            park_run(conn, runs[4], "blocked_on_operator", "which schema?",
                     now=self.now - 2 * DAY + 30 * MIN)
            store.release(conn, runs[4], "failed", "no answer",
                          now=self.now - 2 * DAY + 60 * MIN)
            for run, at in ((1, older + 20 * MIN), (2, newer + 90 * MIN),
                            (3, self.now - DAY), (1, self.now - 20 * DAY)):
                store.record_intervention(conn, runs[run], "approve",
                                          "merge it", now=at)
            with store.transaction(conn):
                for run, rounds in ((1, 1), (2, 2), (3, 3)):
                    conn.execute("UPDATE runs SET reviewRoundCount = ?"
                                 " WHERE id = ?", (rounds, runs[run]))
        finally:
            conn.close()
        self.start()

    def test_each_week_counts_its_merges_rounds_interventions_and_hours(self):
        self.seed_flow()
        code, _, body = self.request("GET", "/flow?weeks=2")
        self.assertEqual(code, 200)
        self.assertEqual(
            [(week["merged"], week["rounds_median"], week["interventions"],
              week["hours"]) for week in body["weeks"]],
            [(1, 1, 1, {"working": 1.5, "waiting": 1.0, "operator": 0}),
             (2, 2.5, 3, {"working": 1.25, "waiting": 1.25, "operator": 0.5})])
        self.assertEqual(body["weeks"][1]["start_ms"] - body["weeks"][0]["start_ms"],
                         7 * DAY)

    def test_parked_time_is_grouped_by_the_reason_that_parked_it(self):
        self.seed_flow()
        code, _, body = self.request("GET", "/flow?weeks=2")
        self.assertEqual(code, 200)
        self.assertEqual(body["waits"], {
            "pending_checks": 0.5, "checks_failing": 0, "thread": 1.0,
            "rounds_used": 0, "behind_main": 0, "main_red": 0, "approval": 0,
            "other": 0.5})

    def test_weeks_outside_one_to_twenty_six_is_400_naming_it(self):
        self.seed()
        self.start()
        for weeks in ("0", "27"):
            with self.subTest(weeks=weeks):
                code, _, body = self.request("GET", f"/flow?weeks={weeks}")
                self.assertEqual(code, 400)
                self.assertIn("weeks", body["error"])


if __name__ == "__main__":
    unittest.main()
