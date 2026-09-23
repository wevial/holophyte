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
from tests.phase_fixture import advance_phase, park_run
from tests.serve_fixture import MIN, ServeTestCase


class FailedAttentionTests(ServeTestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.conn = store.open(str(self.db))
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
                    self.project, {"ticket": "KO-7", "run": self.run})
                self.assertEqual(code, 200)
                self.assertIs(body["ok"], False)
                self.assertEqual(body["detail"],
                                 f"run {self.run} is not KO-7's latest attempt; "
                                 f"run {latest} is")
                self.assertEqual(self.conn.execute(
                    "SELECT COUNT(*) FROM interventions").fetchone()[0], count)
                self.assertEqual(list(self.conn.iterdump()), before)


class PausedAttentionTests(ServeTestCase):
    def test_a_paused_ticket_is_paused_and_a_question_stays_blocked(self):
        from holophyte.stop import stop_if_requested
        self.seed()
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        store.pause(conn, self.run, "reboot the writer")
        with self.assertRaises(store.RunEnded):
            stop_if_requested(conn, self.run, "working")
        project = store.tickets.ensure_project(conn, "team-1", self.target)
        asking = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-8", linear_identifier="KO-8",
            title="ticket 8", acceptance_criteria=["Given 8, then it is worked"],
            verification_commands=["echo ok"])
        store.tickets.transition(conn, asking, "in_flight")
        run = store.claim(conn, project, asking, now=self.now)
        store.tickets.transition(conn, asking, "blocked_on_operator")
        store.set_question(conn, asking, "which API?")
        park_run(conn, run, "blocked_on_operator", "which API?", now=self.now)
        self.start()
        _, _, body = self.request("GET", "/attention")
        items = {item["ticket"]: item for item in body["items"]}
        self.assertEqual(
            {key: items["KO-7"][key] for key in ("kind", "note", "run")},
            {"kind": "paused", "note": "reboot the writer", "run": self.run})
        self.assertEqual((items["KO-8"]["kind"], items["KO-8"]["question"]),
                         ("blocked", "which API?"))


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

    def test_disabled_status_hides_live_runs(self):
        self.seed()
        conn = store.open(self.db)
        try:
            project = store.ensure_project(conn, "team-1", self.target)
            store.set_admission(conn, project, "disabled", "retired")
        finally:
            conn.close()
        self.start()
        code, _, body = self.request("GET", "/status")
        self.assertEqual(code, 200)
        self.assertEqual((body["admission"], body["hold_note"]),
                         ("disabled", "retired"))
        self.assertEqual(body["runs"], [])

    def test_status_reports_project_hold(self):
        self.seed()
        conn = store.open(self.db)
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

class TypedParkAttentionTests(unittest.TestCase):
    def test_reworded_pull_request_stays_a_pull_request(self):
        ticket = store.read.BlockedTicket(
            id=1, linearIdentifier="KO-1", blockedQuestion="Approval needed",
            prUrl="https://github.com/example/repo/pull/7",
            parkKind="pull_request")
        self.assertEqual(holophyte.serve.parked_item(ticket)["kind"], "pr_open")


if __name__ == "__main__":
    unittest.main()


class PauseStatusTests(ServeTestCase):
    def test_pending_request_is_visible_while_turn_is_live(self):
        self.seed()
        conn = store.open(self.db)
        self.addCleanup(conn.close)
        store.pause(conn, self.run, "reboot writer")
        self.start()
        code, _, body = self.request("GET", "/status")
        self.assertEqual(code, 200)
        run = next(r for r in body["runs"] if r["id"] == self.run)
        self.assertEqual((run["phase"], run["stop_requested"]),
                         ("working", "reboot writer"))
        self.assertIsNone(conn.execute("SELECT endedAt FROM runs WHERE id = ?",
                                       (self.run,)).fetchone()[0])


    def test_pending_abort_supersedes_a_pause_in_status(self):
        self.seed()
        conn = store.open(self.db)
        self.addCleanup(conn.close)
        store.pause(conn, self.run, "reboot writer")
        store.abort(conn, self.run, "host going down")
        self.start()
        _, _, body = self.request("GET", "/status")
        run = next(r for r in body["runs"] if r["id"] == self.run)
        self.assertEqual((run["stop_action"], run["stop_requested"]),
                         ("abort", "host going down"))

class PullRequestTitleTests(ServeTestCase):
    """KO-622: the `pr_open` item names the pull request by the title the
    reconcile's read recorded, and the ticket by its own title."""
    URL = "https://github.com/example/repo/pull/2170"
    TITLE = "[People] Let inviters rename pending collaborators"

    def setUp(self):
        super().setUp()
        self.seed()
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        ticket = store.read.ticket_by_identifier(self.conn, "KO-7")
        store.tickets.transition(self.conn, ticket.id, "blocked_on_operator")
        self.conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                          (f"PR open: {self.URL}\nreview requested", ticket.id))
        self.conn.commit()
        park_run(self.conn, self.run, "awaiting_merge_approval", "PR open",
                 candidate_sha="a" * 40, pr_url=self.URL,
                 park_kind="pull_request", now=self.now - MIN)

    def read_status(self, title):
        """One reconcile read of the pull request answering `title`,
        recorded as the reconcile records an unchanged pull request."""
        from holophyte import pr_status, reconcile
        node = {"state": "OPEN", "merged": False, "mergeCommit": None,
                "mergedBy": None, "updatedAt": "2026-09-22T10:00:00Z",
                "title": title}
        with patch.object(pr_status, "graphql", return_value={
                "repository": {"pullRequest": node}}):
            status = pr_status.pull_status(
                None, pr_status.parse_pr_url(self.URL))
        store.record_pr_seen(self.conn, self.run, reconcile._seen(status),
                             parked_only=True, facts_only=True)

    def pr_open(self):
        self.start()
        _, _, body = self.request("GET", "/attention")
        return next(item for item in body["items"]
                    if item["kind"] == "pr_open")

    def test_a_read_title_is_recorded_and_a_later_read_replaces_it(self):
        self.read_status(self.TITLE)
        title = "SELECT prSeenTitle FROM runs WHERE id = ?"
        self.assertEqual(self.conn.execute(title, (self.run,)).fetchone(),
                         (self.TITLE,))
        self.read_status("[People] Rename pending collaborators")
        self.assertEqual(self.conn.execute(title, (self.run,)).fetchone(),
                         ("[People] Rename pending collaborators",))

    def test_the_item_carries_the_recorded_title_and_the_ticket_title(self):
        self.read_status(self.TITLE)
        item = self.pr_open()
        self.assertEqual((item["pr"]["title"], item["title"]),
                         (self.TITLE, "ticket 7"))

    def test_a_run_never_read_has_no_pull_request_title(self):
        item = self.pr_open()
        self.assertEqual((item["pr"]["title"], item["title"]),
                         (None, "ticket 7"))
