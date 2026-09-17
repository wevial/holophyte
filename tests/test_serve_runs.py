"""HTTP run, shipped and startup-outage read regressions."""
from __future__ import annotations

import json
import socket
import sqlite3
import sys
from pathlib import Path
from time import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import test_serve  # noqa: E402 - after the insert; the SLACK tolerance
from serve_fixture import MERGE_SHA, MIN, SEC, ServeTestCase  # noqa: E402

import holophyte.report  # noqa: E402 - after the sys.path insert above
import holophyte.serve_runs  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above

SLACK = test_serve.SLACK


class RunsTests(ServeTestCase):

    def expected_rows(self):
        """The oracle: `report_rows()` over the same store, named by column."""
        conn = store.open(str(self.db))
        try:
            rows = holophyte.report.report_rows(conn)
        finally:
            conn.close()
        keys = ("ticket", "actual_min", "estimate_min", "ratio", "rounds",
                "outcome", "host", "ended_ms", "merge_sha")
        return [dict(zip(keys, row + (ended, sha)))
                for row, ended, sha in zip(rows, self.ended_at(),
                                           self.merge_shas())]

    def ended_at(self):
        """Read stored end times in report order."""
        return self.column("endedAt")

    def merge_shas(self):
        """The oracle for `merge_sha`: `runs.mergeSha` itself, same order."""
        return self.column("mergeSha")

    def column(self, name):
        conn = store.open(str(self.db))
        try:
            return [value for (value,) in conn.execute(
                f"SELECT {name} FROM runs WHERE endedAt IS NOT NULL"
                " ORDER BY endedAt, id")]
        finally:
            conn.close()

    def test_a_merged_run_carries_its_full_merge_sha(self):
        self.seed_ended()
        self.start()

        code, _headers, body = self.request("GET", "/runs")

        self.assertEqual(code, 200)
        self.assertEqual([r["merge_sha"] for r in body["rows"]],
                         [None, None, MERGE_SHA])
        self.assertEqual(len(MERGE_SHA), 40)

    def test_runs_is_the_report_table_as_json(self):
        self.seed_ended()
        self.start()

        code, headers, body = self.request("GET", "/runs")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIsNone(body["limit"])
        expected = self.expected_rows()
        self.assertEqual(len(expected), 3)
        self.assertEqual(body["rows"], expected)
        self.assertEqual([r["ticket"] for r in body["rows"]],
                         ["KO-1", "KO-2", "KO-3"])
        self.assertEqual([r["outcome"] for r in body["rows"]],
                         ["merged", "failed", "merged"])
        self.assertEqual([r["rounds"] for r in body["rows"]], [1, 0, 2])
        self.assertIsNone(body["rows"][2]["estimate_min"])
        self.assertIsNone(body["rows"][2]["ratio"])
        self.assertAlmostEqual(body["rows"][0]["ratio"], 0.5)

    def test_each_run_carries_its_end_as_ended_ms(self):
        self.seed_ended()
        self.start()

        code, _headers, body = self.request("GET", "/runs")

        self.assertEqual(code, 200)
        ended = [r["ended_ms"] for r in body["rows"]]
        self.assertEqual(ended, self.ended_at())
        self.assertTrue(all(isinstance(ms, int) for ms in ended), ended)
        expected = [self.now - (10 - n) * 60 * MIN + took
                    for n, took in enumerate((10 * MIN, 45 * MIN, 15 * MIN))]
        self.assertEqual(ended, expected)

    def test_limit_keeps_the_first_rows_and_a_bad_limit_is_400(self):
        self.seed_ended()
        self.start()

        code, _headers, body = self.request("GET", "/runs?limit=2")
        self.assertEqual(code, 200)
        self.assertEqual(body["limit"], 2)
        self.assertEqual(body["rows"], self.expected_rows()[:2])

        for query in ("limit=0", "limit=abc", "limit=-1", "limit="):
            with self.subTest(query=query):
                code, headers, body = self.request("GET", f"/runs?{query}")
                self.assertEqual(code, 400)
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("error", body)
                self.assertNotIn("rows", body)

    def test_a_configured_host_label_is_every_host_in_the_rows(self):
        self.seed_ended()
        self.start('[report]\nhost_label = "writer-1"\n')

        code, _headers, body = self.request("GET", "/runs")

        self.assertEqual(code, 200)
        self.assertEqual(len(body["rows"]), 3)
        self.assertEqual({r["host"] for r in body["rows"]}, {"writer-1"})
        self.assertNotIn(socket.gethostname(), json.dumps(body))

    def test_a_row_without_a_recorded_host_is_null_under_a_label(self):
        self.seed_ended()
        self.null_host(2)
        self.start('[report]\nhost_label = "writer-1"\n')

        code, _headers, body = self.request("GET", "/runs")

        self.assertEqual(code, 200)
        self.assertEqual([r["host"] for r in body["rows"]],
                         ["writer-1", None, "writer-1"])
        self.assertNotIn("?", json.dumps(body))

    def test_runs_without_a_store_is_503_and_creates_none(self):
        self.start()

        code, _headers, body = self.request("GET", "/runs")

        self.assertEqual(code, 503)
        self.assertIn("no store", body["error"])
        self.assertFalse(self.db.exists())


class ShippedTests(ServeTestCase):
    """`/shipped`: the merge ledger newest end first, paged by `before`."""

    FINDING = {"path": "holophyte/serve.py", "line": 1, "severity": "p2",
               "criterion": None, "message": "a finding"}

    def seed_shipped(self):
        """Seed merges out of id order, plus one failed run."""
        self.now = int(time() * 1000)
        H = 60 * MIN
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            plan = (("KO-1", 10 * H, 9 * H, "merged", (2,), MERGE_SHA),
                    ("KO-2", 8 * H, 7 * H, "failed", (), None),
                    ("KO-3", 6 * H, 1 * H, "merged", (1, 3), "b" * 40),
                    ("KO-4", 5 * H, 4 * H, "merged", (), "c" * 40))
            self.runs = {}
            for ident, started_ago, ended_ago, outcome, findings, sha in plan:
                ticket = store.tickets.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{ident}",
                    linear_identifier=ident, title=f"ticket {ident}",
                    acceptance_criteria=[f"Given {ident}, then it is worked"],
                    verification_commands=["echo ok"], time_box_ms=30 * MIN)
                store.tickets.transition(conn, ticket, "in_flight")
                started = self.now - started_ago
                run = store.claim(conn, project, ticket, now=started)
                for number, count in enumerate(findings, start=1):
                    store.record_review_round(
                        conn, run, number, "changes_requested",
                        "reviewer-model", findings=[self.FINDING] * count,
                        started_at=started + number * MIN)
                store.release(conn, run, outcome, now=self.now - ended_ago,
                              merge_sha=sha,
                              reason="verification failed" if outcome == "failed"
                              else None)
                self.runs[ident] = run
        finally:
            conn.close()

    def test_merged_runs_newest_end_first_with_findings_counted(self):
        self.seed_shipped()
        self.start()

        code, headers, body = self.request("GET", "/shipped")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body["limit"], 50)
        self.assertIsNone(body["next_before"])
        self.assertEqual([r["ticket"] for r in body["rows"]],
                         ["KO-3", "KO-4", "KO-1"])
        self.assertEqual([r["id"] for r in body["rows"]],
                         [self.runs["KO-3"], self.runs["KO-4"],
                          self.runs["KO-1"]])
        self.assertEqual([r["findings"] for r in body["rows"]], [4, 0, 2])
        self.assertEqual([r["rounds"] for r in body["rows"]], [2, 0, 1])
        self.assertEqual([r["merge_sha"] for r in body["rows"]],
                         ["b" * 40, "c" * 40, MERGE_SHA])
        self.assertNotIn("KO-2", json.dumps(body))
        newest = body["rows"][0]
        self.assertEqual(newest["title"], "ticket KO-3")
        self.assertEqual(newest["started_ms"], self.now - 6 * 60 * MIN)
        self.assertEqual(newest["ended_ms"], self.now - 60 * MIN)
        self.assertEqual(newest["actual_min"], 300.0)
        self.assertEqual(newest["estimate_min"], 30.0)
        self.assertEqual(newest["host"], socket.gethostname())

    def test_all_outcomes_preserve_finished_runs_and_bound_the_reason(self):
        self.seed_shipped()
        self.start()

        code, _headers, body = self.request("GET", "/shipped?outcome=all")
        self.assertEqual(code, 200)
        self.assertEqual([r["ticket"] for r in body["rows"]],
                         ["KO-3", "KO-4", "KO-2", "KO-1"])
        self.assertEqual([r["outcome"] for r in body["rows"]],
                         ["merged", "merged", "failed", "merged"])
        self.assertEqual([r["outcome_reason"] for r in body["rows"]],
                         [None, None, "verification failed", None])
        code, _headers, default = self.request("GET", "/shipped")
        self.assertEqual(code, 200)
        self.assertEqual(default["rows"],
                         [body["rows"][0], body["rows"][1], body["rows"][3]])
        code, _headers, explicit = self.request("GET", "/shipped?outcome=merged")
        self.assertEqual(code, 200)
        self.assertEqual(explicit, default)

        conn = store.open(str(self.db))
        try:
            conn.execute("UPDATE runs SET outcomeReason = ? WHERE id = ?",
                         ("x" * 401, self.runs["KO-2"]))
            conn.commit()
        finally:
            conn.close()
        code, _headers, body = self.request("GET", "/shipped?outcome=all")
        self.assertEqual(code, 200)
        self.assertEqual(body["rows"][2]["outcome_reason"], "x" * 400)

    def test_a_client_pages_to_the_end_with_next_before(self):
        self.seed_shipped()
        self.start()

        code, _headers, first = self.request("GET", "/shipped?limit=2")
        self.assertEqual(code, 200)
        self.assertEqual(first["limit"], 2)
        self.assertEqual([r["ticket"] for r in first["rows"]],
                         ["KO-3", "KO-4"])
        self.assertEqual(first["next_before"], self.runs["KO-4"])

        code, _headers, second = self.request(
            "GET", f"/shipped?limit=2&before={first['next_before']}")
        self.assertEqual(code, 200)
        self.assertEqual([r["ticket"] for r in second["rows"]], ["KO-1"])
        self.assertIsNone(second["next_before"])

    def test_bad_parameters_are_400_and_an_unknown_before_is_empty(self):
        self.seed_shipped()
        self.start()

        for query, name in (("limit=0", "limit"), ("limit=x", "limit"),
                            ("before=x", "before"),
                            ("outcome=bogus", "outcome")):
            with self.subTest(query=query):
                code, headers, body = self.request("GET", f"/shipped?{query}")
                self.assertEqual(code, 400)
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertIn(name, body["error"])
                if name == "outcome":
                    self.assertIn("bogus", body["error"])
                self.assertNotIn("rows", body)

        for cursor in ("99999", "-1", "0", str(2 ** 63), str(-(2 ** 63) - 1)):
            with self.subTest(before=cursor):
                code, _headers, body = self.request(
                    "GET", f"/shipped?before={cursor}")
                self.assertEqual(code, 200)
                self.assertEqual(body["rows"], [])
                self.assertIsNone(body["next_before"])

        code, _headers, body = self.request("GET", "/shipped?limit=500")
        self.assertEqual(code, 200)
        self.assertEqual(body["limit"], 200)

    def test_shipped_without_a_store_is_503_and_creates_none(self):
        self.start()

        code, _headers, body = self.request("GET", "/shipped")

        self.assertEqual(code, 503)
        self.assertIn("no store", body["error"])
        self.assertFalse(self.db.exists())


class RunDetailTests(ServeTestCase):
    """Run details include rounds and narrative events."""

    FINDINGS = [
        {"path": "holophyte/serve.py", "line": 12, "severity": "p1",
         "criterion": "AC1", "message": "the route is unmatched"},
        {"path": "docs/reference/http.md", "line": None, "severity": "nit",
         "criterion": None, "message": "no example"},
    ]

    def seed_reviewed(self, cap=None):
        """Seed a merged run with two review rounds and optional round cap."""
        self.now = int(time() * 1000)
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            ticket = store.tickets.mirror_ticket(
                conn, project, linear_issue_id="issue-9",
                linear_identifier="KO-9", title="ticket 9",
                acceptance_criteria=["Given ticket 9, then it is worked"],
                verification_commands=["echo ok"], time_box_ms=25 * MIN)
            store.tickets.transition(conn, ticket, "in_flight")
            started = self.now - 30 * MIN
            self.run = store.claim(conn, project, ticket, now=started)
            # `claim()` writes the first narrative rows itself; the seed's
            # own are appended after and are what the test names.
            self.seeded_events = [
                ("verify", "verify passed", "narrative"),
                ("tool_use", "ran ruff", "detail"),
                ("review", "round 1 asked for changes", "narrative"),
                ("review", "round 2 passed", "narrative"),
            ]
            for n, (kind, summary, level) in enumerate(self.seeded_events):
                store.record_event(conn, self.run, kind, summary, level=level,
                                   now=started + (n + 1) * MIN)
            store.record_review_round(
                conn, self.run, 1, "changes_requested", "reviewer-a",
                findings=self.FINDINGS, started_at=started + 5 * MIN,
                ended_at=started + 8 * MIN)
            store.record_review_round(
                conn, self.run, 2, "pass", "reviewer-b",
                started_at=started + 10 * MIN, ended_at=started + 12 * MIN)
            if cap is not None:
                store.set_review_round_cap(conn, self.run, cap)
            store.release(conn, self.run, "merged", now=started + 20 * MIN,
                          merge_sha=MERGE_SHA)
        finally:
            conn.close()

    def stored_events(self, level):
        """Read stored events of the requested level in sequence order."""
        conn = sqlite3.connect(str(self.db))
        try:
            return conn.execute(
                "SELECT at, kind, summary FROM runEvents"
                " WHERE runId = ? AND level = ? ORDER BY seq",
                (self.run, level)).fetchall()
        finally:
            conn.close()

    def test_rounds_oldest_first_with_findings_as_objects(self):
        self.seed_reviewed()
        self.start()

        code, headers, body = self.request("GET", f"/runs/{self.run}")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual([r["round"] for r in body["rounds"]], [1, 2])
        self.assertEqual([r["verdict"] for r in body["rounds"]],
                         ["changes_requested", "pass"])
        self.assertEqual([r["reviewer_model"] for r in body["rounds"]],
                         ["reviewer-a", "reviewer-b"])
        # Objects, not the stored JSON string, and the two the seed wrote.
        self.assertEqual(body["rounds"][0]["findings"], self.FINDINGS)
        self.assertEqual(body["rounds"][1]["findings"], [])
        self.assertLess(body["rounds"][0]["started_ms"],
                        body["rounds"][0]["ended_ms"])
        self.assertLess(body["rounds"][0]["ended_ms"],
                        body["rounds"][1]["started_ms"])

    def test_events_are_the_narrative_rows_oldest_first_without_detail(self):
        self.seed_reviewed()
        self.start()

        code, _headers, body = self.request("GET", f"/runs/{self.run}")

        self.assertEqual(code, 200)
        got = [(e["at"], e["kind"], e["summary"]) for e in body["events"]]
        self.assertEqual(got, self.stored_events("narrative"))
        # The three the seed wrote are there, in order, among the
        # `phase_change` rows `claim()` and `release()` write themselves; the
        # detail one is not: a store with detail rows still answers the
        # narrative ones.
        narrative = [(k, s) for k, s, level in self.seeded_events
                     if level == "narrative"]
        self.assertEqual([(k, s) for _at, k, s in got if k != "phase_change"],
                         narrative)
        self.assertEqual(len(self.stored_events("detail")), 1)
        self.assertNotIn("ran ruff", [s for _at, _k, s in got])

    def test_a_no_commit_turns_output_is_among_the_events(self):
        """KO-375: include implementer output in the narrative without its payload."""
        self.seed_reviewed()
        conn = store.open(str(self.db))
        try:
            store.record_event(
                conn, self.run, "implementer_output",
                "This contract cannot be met.", level="detail",
                payload="This contract cannot be met.\nno file is named",
                now=self.now - 25 * MIN)
        finally:
            conn.close()
        self.start()

        code, _headers, body = self.request("GET", f"/runs/{self.run}")

        self.assertEqual(code, 200)
        (shown,) = [e for e in body["events"]
                    if e["kind"] == "implementer_output"]
        self.assertEqual(shown["summary"], "This contract cannot be met.")
        self.assertNotIn("payload", shown)
        self.assertNotIn("no file is named", self.raw_body)
        # In its place in the stream: the row was appended last, so the
        # route's `seq` order puts it last.
        self.assertEqual(body["events"][-1]["kind"], "implementer_output")
        self.assertNotIn("ran ruff", [e["summary"] for e in body["events"]])

    def test_the_run_is_the_row_joined_to_its_ticket(self):
        self.seed_reviewed()
        self.start()

        _code, _headers, body = self.request("GET", f"/runs/{self.run}")

        run = body["run"]
        self.assertEqual(run["id"], self.run)
        self.assertEqual(run["ticket"], "KO-9")
        self.assertEqual(run["title"], "ticket 9")
        self.assertEqual(run["phase"], "done")
        self.assertEqual(run["attempt"], 1)
        self.assertEqual(run["outcome"], "merged")
        self.assertEqual(run["time_box_ms"], 25 * MIN)
        self.assertEqual(run["merge_sha"], MERGE_SHA)
        self.assertEqual(run["started_ms"], self.now - 30 * MIN)
        self.assertEqual(run["ended_ms"], self.now - 10 * MIN)
        # No cap stored: a run recorded before the store carried one
        # answers the loop's constant.
        self.assertEqual(run["max_rounds"],
                         holophyte.serve_runs.MAX_ROUNDS)
        self.assertIsInstance(run["max_rounds"], int)
        self.assertIn("branch", run)

    def test_max_rounds_is_the_cap_the_loop_gave_the_run(self):
        """The API reports the persisted round cap."""
        self.seed_reviewed(cap=4)
        self.start()

        _code, _headers, body = self.request("GET", f"/runs/{self.run}")

        self.assertEqual(body["run"]["max_rounds"], 4)
        self.assertNotEqual(body["run"]["max_rounds"],
                            holophyte.serve_runs.MAX_ROUNDS)

    def test_a_live_run_has_a_heartbeat_age_and_an_ended_one_null(self):
        self.seed()  # KO-7, live in `working`, beating 30 s ago
        self.start()

        code, _headers, body = self.request("GET", f"/runs/{self.run}")

        self.assertEqual(code, 200)
        self.assertIsNone(body["run"]["ended_ms"])
        self.assertIsNone(body["run"]["outcome"])
        age = body["run"]["heartbeat_age_ms"]
        self.assertGreaterEqual(age, 30 * SEC)
        self.assertLess(age, 30 * SEC + SLACK)

    def test_an_ended_run_has_no_heartbeat_age(self):
        self.seed_reviewed()
        self.start()

        code, _headers, body = self.request("GET", f"/runs/{self.run}")

        self.assertEqual(code, 200)
        self.assertIsNotNone(body["run"]["ended_ms"])
        self.assertIsNone(body["run"]["heartbeat_age_ms"])

    def test_no_such_run_is_404_and_a_non_integer_is_400(self):
        self.seed()
        self.start()

        code, headers, body = self.request("GET", "/runs/999")
        self.assertEqual(code, 404)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("error", body)
        self.assertEqual(body["run"], 999)

        code, headers, body = self.request("GET", "/runs/abc")
        self.assertEqual(code, 400)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("error", body)

        # Integers no run can have are still integers: 404 carrying `run`
        # as typed, not 400, and not a crash past SQLite's INTEGER range.
        code, _headers, body = self.request("GET", "/runs/-1")
        self.assertEqual(code, 404)
        self.assertEqual(body["run"], "-1")
        code, _headers, body = self.request("GET", "/runs/9223372036854775808")
        self.assertEqual(code, 404)
        self.assertEqual(body["run"], "9223372036854775808")

        # `/runs` and `/runs?limit=N` answer as before.
        code, _headers, body = self.request("GET", "/runs")
        self.assertEqual(code, 200)
        self.assertEqual(body["rows"], [])
        code, _headers, body = self.request("GET", "/runs?limit=2")
        self.assertEqual(code, 200)
        self.assertEqual(body["limit"], 2)

    def test_a_run_id_of_thousands_of_digits_answers_not_disconnects(self):
        # Regression: `int()` refuses strings past Python's digit limit
        # (4300 by default), and the handler used to die on the ValueError
        # and drop the connection. Leading zeros are normalized away, so
        # the padded existing id is that run; an id of that many
        # significant digits is 404 like any other absent one.
        self.seed()
        self.start()
        padding = "0" * 5000

        code, _headers, body = self.request(
            "GET", f"/runs/{padding}{self.run}")
        self.assertEqual(code, 200)
        self.assertEqual(body["run"]["id"], self.run)

        code, headers, body = self.request("GET", f"/runs/1{padding}")
        self.assertEqual(code, 404)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body["run"], f"1{padding}")

    def test_a_target_with_no_store_answers_503(self):
        self.start()

        code, _headers, body = self.request("GET", "/runs/1")

        self.assertEqual(code, 503)
        self.assertIn("error", body)
        self.assertFalse(self.db.exists())



class RouteDownTests(ServeTestCase):
    def test_now_ledger_has_one_outage_and_hides_launches_since_its_start(self):
        from store import launch_backoff

        self.seed()
        conn = store.open(str(self.db))
        try:
            project = conn.execute(
                'SELECT projectId FROM runs WHERE id=?', (self.run,)).fetchone()[0]
            started = self.now - MIN
            store.record_intervention(conn, self.run, 'launch_loop', 'older',
                                      source='supervisor', now=started - 1)
            launch_backoff.failure(conn, project, 'fake-probe: quota exhausted',
                                   started, run_id=self.run)
            store.record_intervention(conn, self.run, 'launch_loop', 'newer',
                                      source='supervisor', now=started + 1)
        finally:
            conn.close()
        self.start()
        code, _, body = self.request(
            'GET', f'/ledger?since={started - 10}&kind=intervention')
        self.assertEqual(code, 200)
        outage, = body['active_outages']
        self.assertEqual(outage['at'], started)
        self.assertIsNone(outage['run'])
        self.assertIn('fake-probe: quota exhausted', outage['text'])
        self.assertIn('since ', outage['text'])
        launches = [r for r in body['entries'] if 'launch_loop:' in r['text']]
        self.assertEqual([r['at'] for r in launches], [started - 1])
