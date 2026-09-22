"""Failed attempts need attention only until a newer attempt exists."""
import io
import sqlite3
import unittest
from unittest.mock import patch

import holophyte.board
import holophyte.reconcile
import holophyte.serve_actions
import holophyte.supervisor
import linear_provider
import store
import store.read
import store.tickets
from provider import LinearProvider
from tests.phase_fixture import advance_phase
from tests.serve_fixture import MIN, ServeTestCase


class FailedAttentionTests(ServeTestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.conn = store.open(str(self.db), migrate="owner")
        self.addCleanup(self.conn.close)
        self.ticket = store.read.ticket_by_identifier(self.conn, "KO-7")
        store.release(self.conn, self.run, "failed", reason="verify red",
                      now=self.now - MIN)
        self.start()

    def mirror_state(self, state):
        project = store.tickets.ensure_project(self.conn, "team-1", self.target)
        task = linear_provider.parse_task({
            "identifier": "KO-7", "id": "issue-7", "title": "ticket 7",
            "description": "", "state": {"name": state}})
        holophyte.board.mirror_task(self.conn, project, task)

    def test_shelved_board_states_hide_failure_until_unshelved(self):
        for state in ("Backlog", "Canceled", "Done", "Todo"):
            with self.subTest(state=state):
                self.mirror_state(state)
                code, _, body = self.request("GET", "/attention")
                self.assertEqual(code, 200)
                self.assertEqual([item["run"] for item in body["items"]
                                  if item["kind"] == "failed"],
                                 [self.run] if state == "Todo" else [])

    def test_provider_refresh_observes_backlog_missing_from_ready_listing(self):
        project = store.tickets.ensure_project(self.conn, "team-1", self.target)
        provider = LinearProvider("project-1", "team-1")
        state = "Backlog"

        def gql(query, variables):
            if query == linear_provider.ISSUE_QUERY:
                self.assertEqual(variables["id"], "KO-7")
                return {"issue": {"identifier": "KO-7", "id": "issue-7",
                                  "title": "ticket 7", "description": "",
                                  "state": {"name": state}}}
            return {"issues": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                    "project": {"issues": {"nodes": [],
                                "pageInfo": {"hasNextPage": False}}}}

        with patch.object(linear_provider, "_gql", side_effect=gql):
            self.assertEqual(provider.ready_issues(), [])
            for refresh in (
                lambda: holophyte.reconcile._reconcile_mirror(
                    self.conn, project, provider),
                lambda: holophyte.supervisor.board_ready(
                    self.conn, project, provider, io.StringIO(), board_ask_ms=0),
            ):
                for state in ("Backlog", "Todo"):
                    refresh()
                    row = store.read.ticket_by_identifier(self.conn, "KO-7")
                    self.assertEqual(self.conn.execute(
                        "SELECT boardState FROM tickets WHERE id = ?",
                        (row.id,)).fetchone()[0], state)
                    self.assertEqual(row.status, "in_flight")
                    code, _, body = self.request("GET", "/attention")
                    self.assertEqual(code, 200)
                    self.assertEqual([i["run"] for i in body["items"]
                                      if i["kind"] == "failed"],
                                     [] if state == "Backlog" else [self.run])
                    if state == "Backlog":
                        before = list(self.conn.iterdump())
                        with self.assertRaisesRegex(store.RequeueRefused, "Backlog"):
                            store.requeue(self.conn, self.ticket.id, "retry")
                        self.assertEqual(list(self.conn.iterdump()), before)

    def test_shelved_parked_ticket_has_no_attention_items(self):
        store.tickets.transition(self.conn, self.ticket.id, "blocked_on_operator")
        self.mirror_state("Backlog")
        code, _, body = self.request("GET", "/attention")
        self.assertEqual(code, 200)
        self.assertEqual(body["items"], [])

    def test_failure_requires_no_active_run_and_an_actionable_status(self):
        for status, active in (("in_flight", self.run), ("needs_spec", None),
                               ("blocked_on_deps", None), ("merged", None),
                               ("abandoned", None)):
            with self.subTest(status=status, active=active):
                self.conn.execute("UPDATE tickets SET status = ?, activeRunId = ?"
                                  " WHERE id = ?", (status, active, self.ticket.id))
                self.conn.commit()
                code, _, body = self.request("GET", "/attention")
                self.assertEqual(code, 200)
                self.assertFalse(any(i["kind"] == "failed" for i in body["items"]))

    def retry(self):
        store.requeue(self.conn, self.ticket.id, "retry verification")
        store.tickets.transition(self.conn, self.ticket.id, "in_flight")
        project = store.tickets.ensure_project(self.conn, "team-1", self.target)
        run = store.claim(self.conn, project, self.ticket.id,
                          now=self.now)
        advance_phase(self.conn, run, "reviewing", now=self.now)
        return run

    def test_newer_live_or_ended_attempt_supersedes_failure(self):
        latest = self.retry()
        ticket = store.read.ticket_by_id(self.conn, self.ticket.id)
        self.assertEqual((ticket.lastRunId, ticket.activeRunId), (self.run, latest))
        for ended in (False, True):
            with self.subTest(ended=ended):
                if ended:
                    store.release(self.conn, latest, "failed", now=self.now)
                code, _, body = self.request("GET", "/attention")
                self.assertEqual(code, 200)
                failed = [item["run"] for item in body["items"]
                          if item["kind"] == "failed"]
                self.assertEqual(failed, [latest] if ended else [])
                if not ended:
                    self.assertEqual(body["level"], "working")
                    self.assertEqual(body["items"], [])

    def test_only_failure_remains_for_in_flight_and_ready(self):
        self.mirror_state("Todo")
        for status in ("in_flight", "ready"):
            with self.subTest(status=status):
                if status == "ready":
                    store.requeue(self.conn, self.ticket.id, "retry later")
                code, _, body = self.request("GET", "/attention")
                self.assertEqual(code, 200)
                self.assertEqual([item["run"] for item in body["items"]
                                  if item["kind"] == "failed"], [self.run])

    def test_superseded_requeue_is_refused_without_writes(self):
        latest = self.retry()
        for ended in (False, True):
            with self.subTest(ended=ended):
                if ended:
                    store.release(self.conn, latest, "failed", now=self.now)
                before = list(self.conn.iterdump())
                count = self.conn.execute(
                    "SELECT COUNT(*) FROM interventions").fetchone()[0]
                code, body = holophyte.serve_actions.requeue_action(
                    self.tgt, {"ticket": "KO-7", "run": self.run})
                self.assertEqual(code, 200)
                self.assertIs(body["ok"], False)
                self.assertEqual(body["detail"],
                                 f"run {self.run} is not KO-7's latest attempt; "
                                 f"run {latest} is")
                self.assertEqual(self.conn.execute(
                    "SELECT COUNT(*) FROM interventions").fetchone()[0], count)
                self.assertEqual(list(self.conn.iterdump()), before)


class HeldStatusTests(ServeTestCase):
    def test_status_before_writer_migrates_version_26_is_read_only(self):
        self.assert_status_before_writer_migrates_is_read_only(26)

    def test_status_before_writer_migrates_version_27_is_read_only(self):
        self.assert_status_before_writer_migrates_is_read_only(27)

    def assert_status_before_writer_migrates_is_read_only(self, version):
        previous = "\n".join(
            line for line in store.schema.SCHEMA.splitlines()
            if not line.strip().startswith(
                ("admission ", "holdNote ", "CHECK (admission IN")))
        with sqlite3.connect(self.db) as conn:
            conn.executescript(previous)
            store.ensure_project(conn, "team-1", self.target)
            conn.execute(f"PRAGMA user_version = {version}")
            before = list(conn.iterdump())
        self.start()
        code, _, body = self.request("GET", "/status")
        self.assertEqual(code, 200)
        self.assertEqual(body["schema_version"], version)
        self.assertEqual((body["admission"], body["hold_note"]),
                         ("enabled", None))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone(), (version,))
            self.assertEqual(list(conn.iterdump()), before)

    def test_status_reports_project_hold(self):
        self.seed()
        conn = store.open(self.db, migrate="owner")
        try:
            project = store.ensure_project(conn, "team-1", self.target)
            store.hold(conn, project, "reboot pending")
        finally:
            conn.close()
        self.start()
        code, _, body = self.request("GET", "/status")
        self.assertEqual(code, 200)
        self.assertEqual(
            (body["admission"], body["hold_note"]), ("held", "reboot pending")
        )

if __name__ == "__main__":
    unittest.main()
