"""Failed attempts need attention only until a newer attempt exists."""
import io
import sqlite3
import unittest
from time import time
from unittest.mock import patch

import holophyte.board.projection
import holophyte.host.reconcile
import holophyte.host.supervisor
import holophyte.serve.serve_actions
import linear_provider
import store
import store.board
import store.read
import store.tickets
from holophyte.loop.failure_triage import triage_failure
from provider import LinearBoard
from tests.failure_triage_fixture import REQUEUE, FakeClaude
from tests.phase_fixture import advance_phase, park_run
from tests.serve_fixture import MIN, ServeTestCase
from tests.test_provider import ticket_body
from tests.test_status import file_stuck_wait


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
        holophyte.board.projection.mirror_task(self.conn, project, task)

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
        provider = LinearBoard("project-1", "team-1")
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
                lambda: holophyte.host.reconcile._reconcile_mirror(
                    self.conn, project, provider),
                lambda: holophyte.host.supervisor.board_ready(
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
                code, body = holophyte.serve.serve_actions.requeue_action(
                    self.project, {"ticket": "KO-7", "run": self.run})
                self.assertEqual(code, 200)
                self.assertIs(body["ok"], False)
                self.assertEqual(body["detail"],
                                 f"run {self.run} is not KO-7's latest attempt; "
                                 f"run {latest} is")
                self.assertEqual(self.conn.execute(
                    "SELECT COUNT(*) FROM interventions").fetchone()[0], count)
                self.assertEqual(list(self.conn.iterdump()), before)


class TriagedFailureAttentionTests(ServeTestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        FakeClaude(self).answer("infra", 0.6)
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        store.release(self.conn, self.run, "failed", reason="verify red",
                      now=self.now - MIN)
        self.start(config=REQUEUE)
        ticket = store.read.ticket_by_identifier(self.conn, "KO-7")
        triage_failure(self.project, self.conn, self.run, ticket.id)

    def test_the_failed_item_carries_the_runs_classification(self):
        code, _, body = self.request("GET", "/attention")
        self.assertEqual(code, 200)
        (item,) = [item for item in body["items"] if item["kind"] == "failed"]
        self.assertEqual((item["run"], item["triage"]), (self.run, {
            "choice": "infra", "confidence": 0.6, "backend": "claude",
            "model": "sonnet", "requeued": False}))


class StrandedAttentionTests(ServeTestCase):
    """KO-7's only run failed 30 hours ago: past the window, it stays only
    while it strands the ticket in flight with no run."""

    HOUR = 60 * MIN
    mirror_state = FailedAttentionTests.mirror_state
    retry = FailedAttentionTests.retry

    def setUp(self):
        super().setUp()
        self.now = int(time() * 1000)
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = store.tickets.ensure_project(self.conn, "team-1", self.target)
        ticket = store.tickets.mirror_ticket(
            self.conn, project, linear_issue_id="issue-7",
            linear_identifier="KO-7", title="ticket 7",
            acceptance_criteria=["Given ticket 7, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MIN)
        store.tickets.transition(self.conn, ticket, "in_flight")
        self.run = store.claim(self.conn, project, ticket,
                               now=self.now - 31 * self.HOUR)
        store.release(self.conn, self.run, "failed", reason="verify red",
                      now=self.now - 30 * self.HOUR)
        self.ticket = store.read.ticket_by_identifier(self.conn, "KO-7")
        self.start()

    def failed_runs(self):
        code, _, body = self.request("GET", "/attention")
        self.assertEqual(code, 200)
        return [item["run"] for item in body["items"] if item["kind"] == "failed"]

    def test_a_stranded_failure_is_listed(self):
        self.assertEqual(self.failed_runs(), [self.run])

    def test_requeued_to_ready_it_drops_out(self):
        store.requeue(self.conn, self.ticket.id, "retry later")
        self.assertEqual(self.failed_runs(), [])

    def test_a_newer_live_attempt_drops_it(self):
        self.retry()
        self.assertEqual(self.failed_runs(), [])

    def test_a_backlog_board_state_hides_it(self):
        self.mirror_state("Backlog")
        self.assertEqual(self.failed_runs(), [])


class PausedAttentionTests(ServeTestCase):
    def test_a_paused_ticket_is_paused_and_a_question_stays_blocked(self):
        from holophyte.loop.stop import stop_if_requested
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
        self.assertEqual(holophyte.serve.server.parked_item(ticket)["kind"], "pr_open")

    def test_the_pull_request_item_carries_its_park_kind(self):
        kinds = []
        for park_kind in ("ci", "pull_request"):
            ticket = store.read.BlockedTicket(
                id=1, linearIdentifier="KO-1",
                blockedQuestion="PR open\npending checks",
                prUrl="https://github.com/example/repo/pull/7",
                parkKind=park_kind)
            item = holophyte.serve.server.parked_item(ticket)
            kinds.append((item["kind"], item.get("park_kind")))
        self.assertEqual(kinds, [("pr_open", "ci"), ("pr_open", "pull_request")])


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

    def read_status(self, title, threads=None, facts_only=None,
                    through_reconcile=False, rate_limit=None, **fields):
        """One reconcile read of the pull request answering `title`,
        recorded as the reconcile records an unchanged pull request, or
        as its first read when GitHub also answers `threads`."""
        from holophyte.host import reconcile
        from holophyte.pr import pr_status
        node = {"state": "OPEN", "merged": False, "mergeCommit": None,
                "mergedBy": None, "updatedAt": "2026-09-22T10:00:00Z",
                "title": title}
        node.update(fields)
        if threads is not None:
            node["threadCount"] = threads
        with patch.object(pr_status, "graphql", return_value={
                "repository": {"pullRequest": node}, "rateLimit": rate_limit}):
            if through_reconcile:
                project = store.tickets.ensure_project(
                    self.conn, "team-1", self.target)
                return reconcile._reconcile_pull_requests(
                    self.project, self.conn, project, None, failed_asked={})
            status = pr_status.pull_status(
                None, pr_status.parse_pr_url(self.URL))
        store.record_pr_seen(self.conn, self.run, reconcile._seen(status),
                             parked_only=True, facts_only=threads is None
                             if facts_only is None else facts_only)

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

    def test_attention_reads_version_50_without_migrating_pull_request_state(self):
        self.read_status(self.TITLE, isDraft=True)
        self.conn.execute("ALTER TABLE runs DROP COLUMN prSeenState")
        self.conn.execute("PRAGMA user_version = 50")
        self.conn.commit()
        before = list(self.conn.iterdump())
        self.start()
        code, _, body = self.request("GET", "/attention")
        self.assertEqual(code, 200)
        item = next(item for item in body["items"] if item["kind"] == "pr_open")
        self.assertEqual(item["run"], self.run)
        self.assertEqual(item["pr"]["title"], self.TITLE)
        self.assertIsNone(item["pr"]["state"])
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_first_draft_read_and_unchanged_ready_read_store_and_serve_state(self):
        for draft, expected, facts_only in ((True, "draft", False),
                                             (False, "ready", True)):
            with self.subTest(state=expected):
                self.read_status(self.TITLE, facts_only=facts_only,
                                 isDraft=draft)
                self.assertEqual(self.pr_open()["pr"]["state"], expected)

    def test_ready_read_during_cooldown_updates_facts_without_waking(self):
        self.start(config='[merge]\npr_poll_sec = 3600\n')
        store.record_event(self.conn, self.run, "pr_draft_held", "held for review")
        for draft, expected, updated in (
                (True, "draft", "2026-09-22T10:00:00Z"),
                (False, "ready", "2026-09-22T10:01:00Z")):
            with self.subTest(state=expected):
                with patch("holophyte.host.reconcile.time",
                           return_value=self.now / 1000):
                    sent = self.read_status(
                        self.TITLE, through_reconcile=True,
                        isDraft=draft, updatedAt=updated)
                self.assertEqual(sent, set())
                self.assertEqual(self.conn.execute(
                    "SELECT prSeenState, prSeenAt, parkKind, lastHeartbeat"
                    " FROM runs WHERE id = ?", (self.run,)).fetchone(),
                    (expected, "2026-09-22T10:00:00Z", "pull_request",
                     self.now - MIN))
                _, _, body = self.request("GET", "/attention")
                item = next(i for i in body["items"] if i["kind"] == "pr_open")
                self.assertEqual(item["pr"]["state"], expected)
        self.assertEqual(self.conn.execute(
            "SELECT kind FROM runEvents WHERE runId = ?"
            " AND kind IN ('ci_wake', 'pr_wake')", (self.run,)).fetchall(), [])

    def test_ready_read_below_budget_floor_updates_facts_without_waking(self):
        from holophyte.host import reconcile

        self.start(config='[merge]\npr_poll_sec = 10\n')
        with patch.object(reconcile, "GITHUB_BUDGET", reconcile.GitHubBudget()), \
                patch.object(reconcile, "time", return_value=self.now / 1000):
            self.read_status(self.TITLE, through_reconcile=True, isDraft=True)
            self.assertEqual(self.pr_open()["pr"]["state"], "draft")
            store.record_event(self.conn, self.run, "pr_draft_held", "held for review")
            sent = self.read_status(
                self.TITLE, through_reconcile=True, isDraft=False,
                updatedAt="2026-09-22T10:01:00Z",
                rate_limit={"remaining": 499, "resetAt": "2099-01-01T00:00:00Z"})
            self.assertEqual(sent, set())
            self.assertEqual(self.conn.execute(
                "SELECT prSeenState, prSeenAt, parkKind, lastHeartbeat"
                " FROM runs WHERE id = ?", (self.run,)).fetchone(),
                ("ready", "2026-09-22T10:00:00Z", "pull_request", self.now - MIN))
            self.assertEqual(self.pr_open()["pr"]["state"], "ready")
            self.assertEqual(self.conn.execute(
                "SELECT kind FROM runEvents WHERE runId = ?"
                " AND kind IN ('ci_wake', 'pr_wake')", (self.run,)).fetchall(), [])

    def test_unread_closed_merged_and_unknown_states_are_served(self):
        self.assertIsNone(self.pr_open()["pr"]["state"])
        for fields, expected in (
                ({"state": "CLOSED", "merged": False, "isDraft": True},
                 "closed"),
                ({"state": "CLOSED", "merged": True, "isDraft": True},
                 "merged"),
                ({"state": "OPEN", "merged": False}, None)):
            with self.subTest(state=expected):
                self.read_status(self.TITLE, **fields)
                self.assertEqual(self.pr_open()["pr"]["state"], expected)

    def test_resolved_threads_are_counted_but_not_open(self):
        self.read_status(self.TITLE, {"totalCount": 4, "nodes": [
            {"isResolved": True} for _ in range(4)]})
        pr = self.pr_open()["pr"]
        self.assertEqual((pr["open_threads"], pr["threads"]), (0, 4))

    def test_a_count_over_only_the_first_page_is_served_as_100_plus(self):
        page = [{"isResolved": True}] * 100
        self.read_status(self.TITLE, {"totalCount": 100, "nodes": page})
        self.read_status(self.TITLE, {"totalCount": 101, "nodes": page},
                         facts_only=True)
        pr = self.pr_open()["pr"]
        self.assertEqual((pr["open_threads"], pr["threads"]), ("100+", 100))


class BlockedParkFactsTests(ServeTestCase):
    def park_question(self, question, park_kind, **facts):
        self.seed()
        with store.open(str(self.db)) as conn:
            ticket = store.read.ticket_by_identifier(conn, "KO-7")
            store.tickets.transition(conn, ticket.id, "blocked_on_operator")
            store.set_question(conn, ticket.id, question)
            park_run(conn, self.run, "awaiting_merge_approval", question,
                     candidate_sha="a" * 40, park_kind=park_kind,
                     now=self.now, **facts)
        self.start()
        code, _, body = self.request("GET", "/attention")
        self.assertEqual(code, 200)
        return next(item for item in body["items"] if item["ticket"] == "KO-7")

    def test_fix_declined_question_keeps_its_text_and_pull_request_facts(self):
        question = ("PR open: https://github.com/example/repo/pull/644\n"
                    "checks failure on the head commit\n"
                    "Fix round made no commit. How should this proceed?")
        item = self.park_question(
            question, "fix_declined",
            pr_url="https://github.com/example/repo/pull/644",
            pr_seen=("2026-09-29T10:00:00Z", 3, None, None, "failure",
                     "changes_requested", None, "ready"))
        self.assertEqual(item["kind"], "blocked")
        self.assertEqual(item["question"], question)
        self.assertEqual(item["park_kind"], "fix_declined")
        self.assertEqual(item["pr"], {
            "number": 644, "checks": "failure", "review": "changes_requested",
            "threads": 3, "open_threads": None, "title": None, "state": "ready"})

    def test_plain_question_has_its_park_kind_and_no_pull_request(self):
        item = self.park_question("Which API should this use?", "question")
        self.assertEqual(item["kind"], "blocked")
        self.assertEqual(item["park_kind"], "question")
        self.assertIsNone(item["pr"])


class CiParkAttentionTests(ServeTestCase):
    URL = "https://github.com/example/repo/pull/31"

    def test_a_run_parked_on_its_checks_is_a_pull_request_in_progress(self):
        self.seed()
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        ticket = store.read.ticket_by_identifier(conn, "KO-7")
        store.tickets.transition(conn, ticket.id, "blocked_on_operator")
        store.set_question(conn, ticket.id, f"PR open: {self.URL}\npending checks")
        park_run(conn, self.run, "awaiting_merge_approval", "pending checks",
                 candidate_sha="a" * 40, pr_url=self.URL, park_kind="ci",
                 pr_seen=("2026-09-29T10:00:00Z", 0, None, None, "pending",
                          None, None, None),
                 now=self.now)
        (blocked,) = store.read.blocked_tickets(conn)
        item = holophyte.serve.server.parked_item(blocked)
        self.assertEqual((item["kind"], item["pr_url"], item["reason"]),
                         ("pr_open", self.URL, "pending checks"))
        self.assertEqual((item["pr"]["number"], item["pr"]["checks"]), (31, "pending"))


class StuckWaitAttentionTests(ServeTestCase):
    def test_a_wait_behind_a_needs_spec_dependency_is_one_stuck_wait(self):
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        store.init(conn)
        file_stuck_wait(conn, store.tickets.ensure_project(
            conn, "team-1", self.target), "NAT")
        self.start()
        code, _, body = self.request("GET", "/attention")
        self.assertEqual(code, 200)
        stuck = [i for i in body["items"] if i["kind"] == "stuck_wait"]
        self.assertEqual(len(stuck), 1, body["items"])
        self.assertEqual(
            {k: stuck[0].get(k) for k in ("ticket", "dependency",
                                          "dependency_state", "reason",
                                          "behind", "level")},
            {"ticket": "NAT-3", "dependency": "NAT-2",
             "dependency_state": "needs_spec",
             "reason": "critic: stale — already shipped by NAT-1",
             "behind": 2, "level": "attention"})


class StuckWaitStatesTests(ServeTestCase):
    URL = "https://github.com/example/repo/pull/41"

    def setUp(self):
        super().setUp()
        self.now = int(time() * 1000)
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.tickets.ensure_project(
            self.conn, "team-1", self.target)

    def file(self, name, depends_on=None, status="ready"):
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{name}",
            linear_identifier=name, title=f"ticket {name}",
            acceptance_criteria=["Given it, then it works"],
            verification_commands=["echo ok"],
            depends_on=[f"issue-{depends_on}"] if depends_on else [])
        for step in {"ready": (), "blocked_on_deps": ("blocked_on_deps",),
                     "in_flight": ("in_flight",),
                     "abandoned": ("in_flight", "abandoned"),
                     "blocked_on_operator": ("in_flight",
                                             "blocked_on_operator")}[status]:
            store.tickets.transition(self.conn, ticket, step)
        return ticket

    def wait_on(self, name, dependency):
        return self.file(name, depends_on=dependency, status="blocked_on_deps")

    def stuck_waits(self):
        self.start()
        code, _, body = self.request("GET", "/attention")
        self.assertEqual(code, 200)
        return {item["ticket"]: (item["dependency"], item["dependency_state"],
                                 item["reason"], item["level"])
                for item in body["items"] if item["kind"] == "stuck_wait"}

    def test_canceled_parked_and_failed_dependencies_are_stuck_waits(self):
        canceled = self.file("NAT-1", status="abandoned")
        store.record_note(self.conn, canceled, "cancel",
                          "Superseded by the new importer.", "cancel:1")
        self.wait_on("NAT-2", "NAT-1")
        parked = self.file("NAT-3", status="blocked_on_operator")
        store.set_question(self.conn, parked,
                           "Which API version should it target?\nv1 or v2")
        self.wait_on("NAT-4", "NAT-3")
        failed = self.file("NAT-5", status="in_flight")
        run = store.claim(self.conn, self.project_id, failed, now=self.now - MIN)
        store.release(self.conn, run, "failed", reason="verify red: 3 tests",
                      now=self.now)
        self.wait_on("NAT-6", "NAT-5")
        self.assertEqual(self.stuck_waits(), {
            "NAT-2": ("NAT-1", "abandoned", "Superseded by the new importer.",
                      "attention"),
            "NAT-4": ("NAT-3", "parked", "Which API version should it target?",
                      "attention"),
            "NAT-6": ("NAT-5", "failed", "verify red: 3 tests", "attention")})

    def test_a_dependency_that_will_move_or_a_shelved_wait_is_no_stuck_wait(self):
        self.file("NAT-1")
        self.wait_on("NAT-2", "NAT-1")
        live = self.file("NAT-3", status="in_flight")
        store.claim(self.conn, self.project_id, live, now=self.now)
        self.wait_on("NAT-4", "NAT-3")
        for n, kind in ((5, "pull_request"), (7, "ci")):
            on_pr = self.file(f"NAT-{n}", status="in_flight")
            run = store.claim(self.conn, self.project_id, on_pr, now=self.now)
            store.tickets.transition(self.conn, on_pr, "blocked_on_operator")
            store.set_question(self.conn, on_pr, f"PR open: {self.URL}")
            park_run(self.conn, run, "awaiting_merge_approval", "checks",
                     candidate_sha="a" * 40, pr_url=self.URL, park_kind=kind,
                     now=self.now)
            self.wait_on(f"NAT-{n + 1}", f"NAT-{n}")
        store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id="issue-NAT-9",
            linear_identifier="NAT-9", title="ticket NAT-9",
            acceptance_criteria=[], verification_commands=[])
        shelved = self.wait_on("NAT-10", "NAT-9")
        store.set_board_state(self.conn, shelved, "Backlog")
        self.assertEqual(self.stuck_waits(), {})

    def test_a_wait_filed_into_the_native_backlog_column_is_no_stuck_wait(self):
        store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id="issue-NAT-1",
            linear_identifier="NAT-1", title="ticket NAT-1",
            acceptance_criteria=[], verification_commands=[])
        text = ticket_body("shelved", verify="echo ok").replace("Depends on: none",
                                              "Depends on: NAT-1")
        shelved = store.board.file_ticket(self.conn, self.project_id, "SHELF",
                                          text, column="backlog")
        self.assertEqual(store.read.ticket_by_identifier(
            self.conn, shelved).status, "blocked_on_deps")
        self.assertEqual(self.stuck_waits(), {})
