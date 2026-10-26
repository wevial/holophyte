"""`GET /history`'s paging, sorting, filters, counts and implementer labels.

Run: python3 -m unittest discover -s tests -p 'test_serve_history_page.py' -v
"""
import json
import unittest
from time import time
from urllib.parse import quote

import store
import store.tickets
from holophyte.serve.contract import History
from tests.phase_fixture import advance_phase, finish_run
from tests.serve_fixture import MIN, ServeTestCase
from tests.test_serve_contract import walk

HOUR = 60 * MIN


class HistoryPageTests(ServeTestCase):
    def setUp(self):
        super().setUp()
        self.now = int(time() * 1000)
        self.since = self.now - 7 * HOUR
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        store.init(conn)
        project = store.tickets.ensure_project(conn, "team-1", self.target)
        self.conn, self.runs = conn, {}

        def claim(n, started, title=None, resumed=False):
            ticket = store.tickets.mirror_ticket(
                conn, project, linear_issue_id=f"issue-{n}",
                linear_identifier=f"KO-{n}", title=title or f"ticket {n}",
                acceptance_criteria=[f"Given {n}, then it is worked"],
                verification_commands=["echo ok"])
            steps = ("blocked_on_deps", "ready") if resumed else ()
            for status in (*steps, "in_flight"):
                store.tickets.transition(conn, ticket, status)
            return ticket, store.claim(conn, project, ticket, now=started)

        def review(run, rounds):
            for number in range(1, rounds + 1):
                store.record_review_round(conn, run, number,
                                          "changes_requested", "reviewer")

        plan = ((11, 6, 60, 2, "merged", None, None),
                (12, 5, 10, 0, "failed", "verify", None),
                (13, 4, 120, 3, "abandoned", None, "Ledger: fold resolutions"),
                (14, 3, 30, 4, "failed", "fix_no_progress", None),
                (15, 2, 45, 0, "merged", None, None))
        for n, hours_ago, minutes, rounds, outcome, kind, title in plan:
            started = self.now - hours_ago * HOUR
            _, self.runs[n] = claim(n, started, title)
            review(self.runs[n], rounds)
            finish_run(conn, self.runs[n], outcome, f"{outcome} run",
                       failure_kind=kind, now=started + minutes * MIN)
        ticket, paused = claim(16, self.now - 100 * MIN)
        store.pause(conn, paused, "reboot the writer", now=self.now - 95 * MIN)
        finish_run(conn, paused, "paused", "reboot the writer",
                   resume_phase="working", now=self.now - 90 * MIN)
        store.walk_ticket(conn, ticket, "blocked_on_operator")
        self.runs["16p"] = paused
        _, self.runs[16] = claim(16, self.now - HOUR, resumed=True)
        advance_phase(conn, self.runs[16], "reviewing", now=self.now - 30 * MIN)
        review(self.runs[16], 1)
        self.names = {run: str(name) for name, run in self.runs.items()}

    def ask(self, params="", code=200):
        got, _, body = self.request("GET", f"/history?since={self.since}{params}")
        self.assertEqual(got, code, body)
        if code == 200:
            walk(body, History, "history")
        return body

    def names_of(self, body):
        return [self.names[row["id"]] for row in body["rows"]]

    def test_an_offset_skips_rows_after_sorting_and_keeps_the_total(self):
        self.start()
        page = self.ask("&limit=2&offset=2")
        self.assertEqual(self.names_of(page), ["15", "14"])
        self.assertEqual((page["total"], page["truncated"], page["offset"]),
                         (7, True, 2))
        for offset in ("7", "99999999999999999999"):
            with self.subTest(offset=offset):
                past = self.ask(f"&offset={offset}")
                self.assertEqual((past["rows"], past["total"]), ([], 7))

    def test_ended_duration_rounds_and_state_sorts_break_ties_by_newer_start(self):
        self.start()
        by_duration = self.ask("&sort=duration&order=desc")
        ended = [self.names[row["id"]] for row in by_duration["rows"]
                 if row["ended_ms"] is not None]
        self.assertEqual((ended[0], ended[-1]), ("13", "12"))
        by_end = self.ask("&sort=ended&order=desc")
        self.assertEqual(self.names_of(by_end)[0], "16")
        by_rounds = self.ask("&sort=rounds&order=asc")
        self.assertEqual(self.names_of(by_rounds)[:3], ["16p", "15", "12"])
        by_state = self.ask("&sort=state&order=asc")
        self.assertEqual(self.names_of(by_state),
                         ["16", "15", "11", "14", "12", "13", "16p"])
        for body, sort, order in ((by_duration, "duration", "desc"),
                                  (by_end, "ended", "desc"),
                                  (by_rounds, "rounds", "asc"),
                                  (by_state, "state", "asc")):
            self.assertEqual((body["sort"], body["order"]), (sort, order))

    def test_state_and_failure_filters_and_counts_that_ignore_their_own_param(self):
        self.start()
        groups = self.ask("&state=failed,abandoned")
        self.assertEqual(self.names_of(groups), ["14", "13", "12"])
        everything = self.ask()
        self.assertEqual(everything["counts"]["state"],
                         {"active": 1, "merged": 2, "failed": 2, "abandoned": 1})
        verify = self.ask("&failure=verify")
        self.assertEqual(self.names_of(verify), ["12"])
        self.assertEqual(
            (verify["counts"]["failure"]["verify"],
             verify["counts"]["failure"]["fix_no_progress"]), (1, 1))

    def test_q_matches_key_or_title_ignoring_case_and_wildcards(self):
        self.start()
        self.assertEqual(self.names_of(self.ask("&q=LEDGER")), ["13"])
        self.assertEqual(self.names_of(self.ask("&q=ko-12")), ["12"])
        self.assertEqual(self.ask(f"&q={quote('50%')}")["rows"], [])
        none = self.ask("&q=ledger&state=merged")
        self.assertEqual((none["rows"], none["total"]), ([], 0))
        self.assertEqual(none["counts"]["state"],
                         {"active": 0, "merged": 0, "failed": 0, "abandoned": 1})

    def test_each_row_names_its_newest_implementer_label(self):
        for run, label in ((self.runs[11], "claude opus"),
                           (self.runs[12], "claude opus"),
                           (self.runs[12], "codex gpt-6-astra")):
            store.record_event(self.conn, run, "implementer_profile",
                               f"implementer default: {label}", level="detail",
                               payload=json.dumps({"profile": None,
                                                   "label": label}))
        self.start()
        body = self.ask()
        labels = {self.names[row["id"]]: row["implementer"]
                  for row in body["rows"]}
        self.assertEqual(labels, {"11": "claude opus", "12": "codex gpt-6-astra",
                                  "13": None, "14": None, "15": None,
                                  "16": None, "16p": None})
        self.assertEqual(body["implementers"], ["claude opus", "codex gpt-6-astra"])
        self.assertEqual(body["counts"]["implementer"],
                         {"claude opus": 1, "codex gpt-6-astra": 1})
        self.assertEqual(
            self.names_of(self.ask("&implementer=codex%20gpt-6-astra")), ["12"])
        self.assertEqual(self.names_of(self.ask(
            "&implementer=claude%20opus,codex%20gpt-6-astra")), ["12", "11"])

    def test_a_bad_param_is_400_naming_it_and_its_value(self):
        self.start()
        for param, value, bad in (("sort", "size", "size"),
                                  ("order", "up", "up"),
                                  ("offset", "-1", "-1"),
                                  ("offset", "x", "x"),
                                  ("state", "open", "open"),
                                  ("state", "failed,open", "open"),
                                  ("failure", "verify,nope", "nope")):
            with self.subTest(param=param, value=value):
                body = self.ask(f"&{param}={value}", code=400)
                self.assertIn(param, body["error"])
                self.assertIn(repr(bad), body["error"])

    def test_limit_is_served_up_to_five_thousand(self):
        self.start()
        body = self.ask("&limit=9000")
        self.assertEqual((body["limit"], len(body["rows"])), (5000, 7))


if __name__ == "__main__":
    unittest.main()
