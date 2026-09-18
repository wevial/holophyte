"""HTTP run, shipped and startup-outage read regressions."""
from __future__ import annotations

import io
import json
import socket
import sqlite3
import sys
from pathlib import Path
from time import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_serve  # noqa: E402 - after the insert; the SLACK tolerance
from babysit_fixture import OperatorNoteCase  # noqa: E402
from bot_thread_fixture import BotFindingCases  # noqa: E402
from fake_agent import APPROVE, Commit, Idle  # noqa: E402
from loop_fixture import MergeModeFixture  # noqa: E402
from pool_restart_cases import PreviousBuildCases  # noqa: E402
from serve_fixture import MERGE_SHA, MIN, SEC, ServeTestCase  # noqa: E402

import holophyte.loop  # noqa: E402 - after the sys.path insert above
import holophyte.pullrequest  # noqa: E402 - after the sys.path insert above
import holophyte.report  # noqa: E402 - after the sys.path insert above
import holophyte.serve_runs  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from tests.ticket_url_fixture import assert_api_url

SLACK = test_serve.SLACK


class LivePullRequestTests(MergeModeFixture):
    def test_a_fresh_pull_request_is_visible_on_the_live_run(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        observed = []

        def open_and_observe(target, conn, run_id, *args, **kwargs):
            url = holophyte.pullrequest._open_pr(
                target, conn, run_id, *args, **kwargs)
            observed.append(holophyte.serve_runs.run_detail(
                target, str(run_id)))
            return url

        with patch.object(holophyte.loop, "_open_pr", open_and_observe):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=self.provider())

        self.assertEqual(len(observed), 1)
        code, body = observed[0]
        self.assertEqual(code, 200)
        run = body["run"]
        self.assertIsNone(run["ended_ms"])
        self.assertEqual(run["phase"], "merge_gate")
        self.assertEqual(run["pr_url"], self.URL)

    def test_a_resumed_pull_request_is_visible_on_the_live_run(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        self.loop(Commit("candidate"), APPROVE, Idle(""),
                  provider=self.provider())
        holophyte.operator.babysit_ticket(
            self.tgt, "KO-131", "look again", out=io.StringIO())
        observed = []
        babysit = holophyte.pullrequest.babysitter._babysit

        def observe_resume(target, conn, run_id, *args, **kwargs):
            observed.append(holophyte.serve_runs.run_detail(target, str(run_id)))
            return babysit(target, conn, run_id, *args, **kwargs)

        with patch.object(holophyte.pullrequest.babysitter, "_babysit",
                          observe_resume):
            self.loop(provider=self.provider())

        self.assertEqual(len(observed), 1)
        code, body = observed[0]
        self.assertEqual(code, 200)
        run = body["run"]
        self.assertEqual(run["id"], 2)
        self.assertIsNone(run["ended_ms"])
        self.assertEqual(run["phase"], "merge_gate")
        self.assertEqual(run["pr_url"], self.URL)


class OperatorNoteDetailTests(OperatorNoteCase, MergeModeFixture):
    def test_consuming_round_lists_private_note_and_report_cites_event(self):
        run_id, event_id = self.operator_note_pass(False)
        code, body = holophyte.serve_runs.run_detail(self.tgt, str(run_id))
        self.assertEqual(code, 200)
        notes = [n for r in body["rounds"] for n in r["operator_notes"]]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["kind"], "operator_note")
        self.assertEqual(notes[0]["note"], "remove the subheader")
        self.assertEqual(notes[0]["event_id"], event_id)
        with store.open(str(self.tgt.store_path)) as conn:
            report = "\n".join(holophyte.report.report_lines(conn))
        self.assertIn(f"Run {run_id} round 1: operator_note event {event_id}", report)
        self.assertIn("remove the subheader", report)


class RunsTests(PreviousBuildCases, ServeTestCase):
    def test_url_on_live_attention_board_detail_and_shipped(self):
        assert_api_url(self)

    def expected_rows(self):
        conn = store.open(str(self.db))
        try:
            rows = holophyte.report.report_rows(conn)
        finally:
            conn.close()
        keys = ("ticket", "actual_min", "estimate_min", "ratio", "rounds",
                "outcome", "host", "ended_ms", "merge_sha", "wall_min")
        return [dict(zip(keys, row + (ended, sha, (ended - started) / MIN)),
                     ticket_url=None)
                for row, ended, sha, started in zip(
                    rows, self.ended_at(), self.merge_shas(), self.column("startedAt"))]

    def ended_at(self):
        return self.column("endedAt")

    def merge_shas(self):
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

    FINDING = {"path": "holophyte/serve.py", "line": 1, "severity": "p2",
               "criterion": None, "message": "a finding"}

    def seed_shipped(self):
        """Seed out-of-id-order merges and a failure with measured work and findings."""
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
                conn.execute("UPDATE runs SET workingMs = ? WHERE id = ?",
                             (started_ago - ended_ago, run))
                conn.commit()
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


class RunDetailTests(BotFindingCases, ServeTestCase):
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
            # Append the seed's events after claim's narrative rows.
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

    def test_mentioned_thread_is_an_instruction_separate_from_findings(self):
        from holophyte import babysitter, pr, thread_mentions
        from holophyte.review import parse_findings

        self.seed_reviewed()
        mentioned = thread_mentions.classify(pr.Thread(
            "T1", "app.py", 1, "operator", "@holophyte use the path tokenId",
            "https://github.com/example/repo/pull/1#discussion_r1"), "holophyte")
        finding = pr.Thread("T2", "app.py", 2, "reviewer", "Handle empty tokens",
                            "https://github.com/example/repo/pull/1#discussion_r2")
        reply = babysitter.round_reply(
            pr.PullRequest("github.com", "example", "repo", 1,
                           "https://github.com/example/repo/pull/1"),
            1, (mentioned, finding),
            {1: ("ADDRESS", mentioned.request), 2: ("ADDRESS", "handle empty tokens")},
            "success", "abc123")
        conn = store.open(str(self.db))
        try:
            store.record_review_round(conn, self.run, 3, "changes_requested",
                                      "github:reviewer", findings=parse_findings(reply))
        finally:
            conn.close()
        self.start()
        code, _, body = self.request("GET", f"/runs/{self.run}")
        self.assertEqual(code, 200)
        rnd = body["rounds"][-1]
        self.assertEqual(len(rnd["instructions"]), 1)
        self.assertIn("use the path tokenId", rnd["instructions"][0]["message"])
        self.assertEqual(len(rnd["findings"]), 1)
        self.assertIn("Handle empty tokens", rnd["findings"][0]["message"])

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
        # Narrative seed events remain ordered among the phase changes.
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
        # Sequence order puts the appended event last.
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
        # Older runs without a stored cap use the loop's constant.
        self.assertEqual(run["max_rounds"],
                         holophyte.serve_runs.MAX_ROUNDS)
        self.assertIsInstance(run["max_rounds"], int)
        self.assertIn("branch", run)

    def test_max_rounds_is_the_cap_the_loop_gave_the_run(self):
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
        # Impossible integer IDs return 404, including SQLite overflow.
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


class ActiveRoutesTests(ServeTestCase):
    def test_status_shows_only_live_fallbacks_and_resets_to_primary(self):
        from holophyte.agent_routes import reset
        from holophyte.agents import ProbeResult, activate_fallback
        from holophyte.target import Target
        self.seed()
        target = Target.locate(self.target)
        target._config = {'agents': {'implementer': 'codex exec',
                                    'implementer_fallback': 'devin -p'}}
        self.addCleanup(reset, target)
        conn = store.open(str(self.db))
        try:
            run, = conn.execute('SELECT id FROM runs LIMIT 1').fetchone()
            activate_fallback(target, 'implement', 'quota exhausted', conn, run,
                              probe=ProbeResult(['devin', '-p'], 0, 'ready', 90))
        finally:
            conn.close()
        # The daemon constructs its own Target, proving the indicator is not
        # accidentally reading the loop's in-memory route map.
        self.start()
        code, _, body = self.request('GET', '/status')
        self.assertEqual(code, 200)
        self.assertEqual(body['active_routes']['implementer'],
                         {'command': 'devin', 'fallback': 'devin'})
        self.assertNotIn('fallback', body['active_routes']['reviewer'])
        reset(target)
        _, _, body = self.request('GET', '/status')
        self.assertNotIn('fallback', body['active_routes']['implementer'])


class MigrationFeedTests(ServeTestCase):
    def test_now_includes_one_neutral_migration_and_respects_filters(self):
        self.seed()
        conn = store.open(str(self.db))
        try:
            store.record_intervention(conn, self.run, "migrate", "operator note")
        finally:
            conn.close()
        target = holophyte.target.Target.locate(self.target)
        status, body = holophyte.serve_runs.ledger(target, "since=0")
        self.assertEqual(status, 200)
        rows = [r for r in body["entries"] if r.get("action") == "migrate"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tone"], "neutral")
        self.assertIsNone(rows[0]["run"])
        self.assertIn("store schema", rows[0]["text"])
        for query in ("since=0&ticket=KO-7", "since=0&kind=merge",
                      f"since={rows[0]['at'] + 1}"):
            _, filtered = holophyte.serve_runs.ledger(
                target, query)
            self.assertFalse(any(r.get("action") == "migrate"
                                 for r in filtered["entries"]))
