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
from holophyte.pr import github, pr_ready  # noqa: E402 - after the insert
from holophyte.pr.pr_status import parse_pr_url  # noqa: E402 - after the insert

PR_URL = "https://github.com/example/repo/pull/31"
NODE_ID = "PR_kwDOnode31"


class FakeGitHub:
    """Answers the pull request's REST read and records each GraphQL call."""

    def __init__(self):
        self.graphql = []

    def __call__(self, target, host, method, path, payload, accept=None):
        if path == "graphql":
            self.graphql.append(payload)
            return {"data": {"markPullRequestReadyForReview":
                             {"pullRequest": {"isDraft": False}}}}
        if (method, path) == ("GET", "repos/example/repo/pulls/31"):
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


if __name__ == "__main__":
    unittest.main()
