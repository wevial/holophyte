"""A parked pull request's rounds and unmerged dependencies, and a live run's
pull request, as `/attention` and `/status` serve them from the store.

Run: python3 -m unittest discover -s tests -p 'test_serve_pr_facts.py' -v
"""
import unittest

import holophyte.serve.views
import store
import store.tickets
from holophyte.config.project import Project
from tests.phase_fixture import park_run
from tests.serve_fixture import ServeTestCase

NOW = 1_750_000_000_000
PULL = "https://github.com/example/repo/pull/{}"


class PullRequestFactsTests(ServeTestCase):
    def setUp(self):
        super().setUp()
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.tickets.ensure_project(self.conn, "team-1",
                                                    self.target)

    def ticket(self, n, depends_on=()):
        return store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{n}",
            linear_identifier=f"KO-{n}", title=f"ticket {n}",
            acceptance_criteria=["Given it, then it works"],
            verification_commands=["echo ok"], depends_on=list(depends_on))

    def claim(self, ticket):
        store.tickets.transition(self.conn, ticket, "in_flight")
        return store.claim(self.conn, self.project_id, ticket, now=NOW - 60_000)

    def park_on_pull_request(self, n, depends_on=(), verdicts=()):
        ticket = self.ticket(n, depends_on)
        run = self.claim(ticket)
        for number, verdict in enumerate(verdicts, 1):
            store.record_review_round(self.conn, run, number, verdict,
                                      "reviewer")
        store.tickets.transition(self.conn, ticket, "blocked_on_operator")
        park_run(self.conn, run, "awaiting_merge_approval", "PR open",
                 pr_url=PULL.format(n), park_kind="pull_request",
                 now=NOW - 30_000)
        return run

    def pr_open(self):
        code, body = holophyte.serve.views.attention(
            Project.locate(self.target), now=NOW)
        self.assertEqual(code, 200)
        return {item["ticket"]: item for item in body["items"]
                if item["kind"] == "pr_open"}

    def test_a_parked_pr_counts_scored_rounds_and_waits_on_unmerged_deps(self):
        first = self.claim(self.ticket(1))
        store.set_pull_request(self.conn, first, PULL.format(7))
        merged = self.ticket(3)
        store.tickets.transition(self.conn, merged, "in_flight")
        store.tickets.transition(self.conn, merged, "merged")
        self.park_on_pull_request(
            2, depends_on=("issue-1", "issue-3"),
            verdicts=("pass", "changes_requested", "error"))
        item = self.pr_open()["KO-2"]
        self.assertEqual(item["round"], 2)
        self.assertEqual(item["waits_on"],
                         [{"ticket": "KO-1", "pr_url": PULL.format(7)}])

    def test_no_dependency_waits_on_nothing_and_one_never_run_has_no_pull_request(self):
        self.park_on_pull_request(4)
        self.ticket(6)
        self.park_on_pull_request(5, depends_on=("issue-6",))
        items = self.pr_open()
        self.assertEqual(items["KO-4"]["waits_on"], [])
        self.assertEqual(items["KO-5"]["waits_on"],
                         [{"ticket": "KO-6", "pr_url": None}])

    def test_a_live_run_names_its_pull_request_and_one_without_has_nulls(self):
        with_pull = self.claim(self.ticket(8))
        store.set_phase(self.conn, with_pull, "working", now=NOW - 50_000)
        store.set_pull_request(self.conn, with_pull, PULL.format(8))
        store.record_pr_seen(self.conn, with_pull,
                             (NOW, 5, 3, None, "failure", None, None, "ready"))
        without = self.claim(self.ticket(9))
        store.set_phase(self.conn, without, "working", now=NOW - 50_000)
        code, body = holophyte.serve.views.status(
            Project.locate(self.target), now=NOW)
        self.assertEqual(code, 200)
        runs = {run["id"]: run for run in body["runs"]}
        self.assertEqual(runs[with_pull]["pr_url"], PULL.format(8))
        self.assertEqual((runs[with_pull]["pr"]["number"],
                          runs[with_pull]["pr"]["checks"],
                          runs[with_pull]["pr"]["open_threads"]),
                         (8, "failure", 3))
        self.assertEqual([runs[without][key] for key
                          in ("pr_url", "pr", "woke_ms")], [None, None, None])


if __name__ == "__main__":
    unittest.main()
