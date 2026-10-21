"""`POST /actions/mark-ready`: a run parked on a draft pull request is marked
ready for review through GitHub's `markPullRequestReadyForReview`, and every
other run is refused without a GitHub call.

Run: python3 -m unittest discover -s tests -p 'test_serve_actions_mark_ready.py' -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_serve  # noqa: E402 - after the insert; TokenTests' TOKEN and BEARER
from serve_fixture import ServeTestCase  # noqa: E402 - after the insert

import store  # noqa: E402 - after the sys.path insert above
import store.read  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from holophyte.loop.gates import InfraFailure  # noqa: E402 - after the insert
from holophyte.pr import github, pr_ready  # noqa: E402 - after the insert
from holophyte.pr.pr_status import parse_pr_url  # noqa: E402 - after the insert

PR_URL = "https://github.com/example/repo/pull/31"
NODE_ID = "PR_kwDOnode31"


class FakeGitHub:
    """Answers the pull request's REST read and records each GraphQL call."""

    def __init__(self):
        self.graphql = []
        self.refuse = None

    def __call__(self, target, host, method, path, payload, accept=None):
        if path == "graphql":
            self.graphql.append(payload)
            if self.refuse == "mutation":
                return {"errors": [{"message": "Resource not accessible"}]}
            return {"data": {"markPullRequestReadyForReview":
                             {"pullRequest": {"isDraft": False}}}}
        if (method, path) == ("GET", "repos/example/repo/pulls/31"):
            if self.refuse == "read":
                raise InfraFailure("gh api answered 502")
            return {"node_id": NODE_ID, "number": 31, "draft": True}
        raise AssertionError(f"unexpected GitHub call {method} {path}")


class MarkReadyTests(ServeTestCase):
    TOKEN = test_serve.TokenTests.TOKEN
    BEARER = test_serve.TokenTests.BEARER

    def setUp(self):
        super().setUp()
        self.seed()
        path = self.root / "serve.token"
        path.write_text(self.TOKEN + "\n")
        path.chmod(0o600)
        self.start(f'[serve]\ntoken_file = "{path}"\nactions = true\n')
        self.github = FakeGitHub()
        calls = patch.object(github, "_call", side_effect=self.github)
        calls.start()
        self.addCleanup(calls.stop)

    def park(self, state, held=True):
        with store.open(str(self.db)) as conn:
            for phase in ("verifying", "reviewing", "merge_gate"):
                store.set_phase(conn, self.run, phase)
            store.park(conn, self.run, "awaiting_merge_approval", pr_url=PR_URL,
                       park_kind="pull_request",
                       pr_seen=("2026-10-20T10:00:00Z", 0, 0, None, "success",
                                None, "the title", state))
            ticket = store.read.ticket_by_identifier(conn, "KO-7").id
            store.tickets.transition(conn, ticket, "blocked_on_operator")
            if held:
                pr_ready.record_hold(conn, self.run, parse_pr_url(PR_URL),
                                     "abc123")

    def mark_ready(self):
        return self.request("POST", "/actions/mark-ready", self.BEARER,
                            body={"run": self.run})

    def park_approved(self):
        self.park("draft")
        with store.open(str(self.db)) as conn:
            store.record_event(conn, self.run, "approval_parked",
                               "the approval of abc123 waits with the park",
                               level="detail", payload='{"sha": "abc123"}')

    def wake(self):
        """The run's seen state, its `ci_wake` summaries and its babysit
        interventions as `(id, source)`."""
        with store.open(str(self.db)) as conn:
            (seen,) = conn.execute("SELECT prSeenState FROM runs WHERE id = ?",
                                   (self.run,)).fetchone()
            woken = conn.execute("SELECT summary FROM runEvents WHERE runId = ?"
                                 " AND kind = 'ci_wake'", (self.run,)).fetchall()
            babysits = conn.execute(
                "SELECT id, source FROM interventions WHERE runId = ?"
                " AND action = 'babysit'", (self.run,)).fetchall()
        return seen, woken, babysits

    def ready_events(self):
        with store.open(str(self.db)) as conn:
            return conn.execute("SELECT summary FROM runEvents WHERE runId = ?"
                                " AND kind = 'pr_ready'",
                                (self.run,)).fetchall()

    def test_a_held_draft_is_marked_ready_once_and_records_pr_ready(self):
        self.park("draft")
        code, _, reply = self.mark_ready()
        self.assertEqual(code, 200, reply)
        self.assertIs(reply["ok"], True, reply)
        (sent,) = self.github.graphql
        self.assertIn("markPullRequestReadyForReview", sent["query"])
        self.assertEqual(sent["variables"], {"pull": NODE_ID})
        self.assertEqual(self.ready_events(), [
            (f"{PR_URL} marked ready for review from the console by"
             " maintainer",)])

    def test_a_ready_or_unparked_run_is_refused_without_a_mutation(self):
        unparked = self.mark_ready()
        self.park("ready")
        ready = self.mark_ready()
        for name, (code, _, reply) in (("not parked", unparked),
                                       ("ready", ready)):
            with self.subTest(name):
                self.assertEqual(code, 200, reply)
                self.assertIs(reply["ok"], False, reply)
                self.assertEqual(
                    reply["detail"],
                    f"run {self.run} is not a draft held for review")
        self.assertEqual(self.github.graphql, [])
        self.assertEqual(self.ready_events(), [])

    def test_a_draft_parked_without_a_hold_is_refused_without_a_mutation(self):
        self.park("draft", held=False)
        code, _, reply = self.mark_ready()
        self.assertEqual(code, 200, reply)
        self.assertEqual(reply["detail"],
                         f"run {self.run} is not a draft held for review")
        self.assertEqual(self.github.graphql, [])
        self.assertEqual(self.ready_events(), [])

    def test_marking_ready_wakes_the_run_back_to_the_babysitter(self):
        self.park("draft")
        code, _, reply = self.mark_ready()
        self.assertEqual(code, 200, reply)
        seen, woken, babysits = self.wake()
        self.assertEqual(seen, "ready")
        self.assertEqual(woken, [(f"{PR_URL} was marked ready for review",)])
        self.assertEqual([source for _, source in babysits], ["human"])
        with store.open(str(self.db)) as conn:
            ticket = store.read.ticket_by_id(
                conn, store.read.ticket_by_identifier(conn, "KO-7").id)
        self.assertEqual((ticket.status, ticket.activeRunId), ("ready", None))

    def test_the_answer_names_the_babysit_and_the_ticket_reads_queued(self):
        self.park("draft")
        _, _, before = self.request("GET", "/attention", self.BEARER)
        self.assertIn(("pr_open", self.run), [
            (item["kind"], item.get("run")) for item in before["items"]])
        _, _, reply = self.mark_ready()
        (_, _, [(intervention, _)]) = self.wake()
        self.assertIs(reply["ok"], True, reply)
        self.assertEqual(reply["recorded"], intervention)
        self.assertEqual(reply["detail"],
                         f"{PR_URL} marked ready for review; run {self.run}"
                         " sent back to the babysitter")
        _, _, attention = self.request("GET", "/attention", self.BEARER)
        self.assertNotIn(("pr_open", self.run), [
            (item["kind"], item.get("run")) for item in attention["items"]])
        _, _, status = self.request("GET", "/status", self.BEARER)
        self.assertIn(("KO-7", PR_URL), [
            (row["ticket"], row["pr_url"]) for row in status["queued"]])

    def test_a_parked_approval_leaves_the_wake_to_the_supervisor(self):
        self.park_approved()
        code, _, reply = self.mark_ready()
        self.assertIs(reply["ok"], True, reply)
        self.assertEqual(self.wake(), ("ready", [], []))
        with store.open(str(self.db)) as conn:
            (phase,) = conn.execute("SELECT phase FROM runs WHERE id = ?",
                                    (self.run,)).fetchone()
        self.assertEqual(phase, "awaiting_merge_approval")

    def assert_refused_unwoken(self, state, refuse=None, held=True):
        self.github.refuse = refuse
        self.park(state, held=held)
        _, _, reply = self.mark_ready()
        self.assertIs(reply["ok"], False, reply)
        self.assertEqual(self.wake(), (state, [], []))

    def test_a_run_not_held_as_a_draft_is_not_woken(self):
        self.assert_refused_unwoken("draft", held=False)

    def test_a_refused_mutation_leaves_the_draft_unwoken(self):
        self.assert_refused_unwoken("draft", refuse="mutation")

    def test_a_failed_pull_request_read_leaves_the_draft_unwoken(self):
        self.assert_refused_unwoken("draft", refuse="read")

if __name__ == "__main__":
    unittest.main()
