"""Failed attempts need attention only until a newer attempt exists."""
import unittest

import holophyte.board
import holophyte.serve_actions
import linear_provider
import store
import store.read
import store.tickets
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
        store.set_phase(self.conn, run, "reviewing", now=self.now)
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


if __name__ == "__main__":
    unittest.main()
