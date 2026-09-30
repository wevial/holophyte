"""A parked pull request's fate on GitHub and on the tick."""
from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    Commit,
    Idle,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    TICK,
    FakePool,
    MergeModeFixture,
    StubProvider,
    a_task,
)

import holophyte.cli.operator  # noqa: E402 - after the sys.path insert above
import holophyte.loop.pool  # noqa: E402 - after the sys.path insert above
import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pr_status  # noqa: E402 - after the sys.path insert above


class ParkedPullRequestTests(MergeModeFixture):
    # What GitHub says about a parked pull request when the reconcile asks
    # (`pr_status.PULL_QUERY`'s node): merged by a coworker, closed
    # unmerged, open.
    MERGED_PULL = {"state": "MERGED", "merged": True,
                   "mergeCommit": {"oid": MergeModeFixture.MERGE_SHA},
                   "mergedBy": {"login": "coworker"}}

    CLOSED_PULL = {"state": "CLOSED", "merged": False, "mergeCommit": None,
                   "mergedBy": None}

    OPEN_PULL = {"state": "OPEN", "merged": False, "mergeCommit": None,
                 "mergedBy": None}

    def fake_client(self, *answers, rate=None):
        """The reconcile's GitHub, faked: `holophyte.pr.pr_status.graphql`
        answers each ask with the next of `answers` (the last one
        forever) and
        records the pull request and variables it was asked about. An
        answer that is an exception is raised instead: GitHub down.
        `rate` is the `rateLimit` node every answer carries, when one
        does. Only the pull-status read is faked here: the babysitter's own
        reads and writes still go to the scripted `gh`."""
        asked = []
        real = holophyte.pr.github.graphql

        def graphql(target, pull, query, variables):
            if "mergedBy" not in query:
                return real(target, pull, query, variables)
            asked.append((pull.url, query, variables))
            node = answers[min(len(asked), len(answers)) - 1]
            if isinstance(node, Exception):
                raise node
            data = {"repository": {"pullRequest": node}}
            if rate is not None:
                data["rateLimit"] = rate
            return data

        patcher = patch.object(holophyte.pr.pr_status, "graphql", graphql)
        patcher.start()
        self.addCleanup(patcher.stop)
        return asked

    def parked_on_pr(self, extra=""):
        """A run parked on its pull request under `approve = "human"`, the
        state every reconcile test starts from; `extra` is further config
        text appended after the `[merge]` table."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n' + extra)
        self.fake_route()
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  provider=self.provider())
        self.assertEqual(self.read("SELECT phase, prUrl FROM runs"),
                         [("awaiting_merge_approval", self.URL)])

    def test_a_pull_request_merged_on_github_ships_its_parked_run(self):
        """KO-359: a person merged the pull request on GitHub instead of
        saying `--approve`. The next pass asks GitHub once, and the merge
        is the approval: the parked run ends `merged` with the pull
        request's merge commit as its `mergeSha`, the ticket is `merged`
        and the board saw Done, the ledger names who merged it, the local
        branch is gone and the findings window, where a target renders
        one, shows the run."""
        self.parked_on_pr('[report]\nfindings = "repo"\n')
        asked = self.fake_client(self.MERGED_PULL)
        provider = StubProvider()

        out = self.main_output(provider=provider)

        self.assertEqual([(url, v["number"]) for url, _, v in asked],
                         [(self.URL, 7)])
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha, prUrl FROM runs"),
            [("done", "merged", self.MERGE_SHA, self.URL)])
        self.assertEqual(
            self.read("SELECT status, blockedQuestion FROM tickets"),
            [("merged", None)])
        self.assertEqual(provider.states, [("iss-131", "Done")])
        self.assertEqual(self.read('SELECT "action" FROM interventions'
                                   " WHERE action != 'migrate'"),
                         [("approve",)])
        (merge_line,) = [text for (text,) in self.read(
            "SELECT text FROM ledger WHERE kind = 'merge'")]
        self.assertIn("coworker", merge_line)
        self.assertIn(self.MERGE_SHA, merge_line)
        self.assertIn(f"{self.URL} was merged on GitHub by coworker", out)
        self.assertNotIn(BRANCH, self.branches())
        self.assertIn("KO-131", (self.target / "FINDINGS.md").read_text())
        self.assertEqual(self.subjects(), ["base"])  # local main not moved

    def test_a_merged_pull_request_ships_when_linear_already_says_done(self):
        """Review of KO-359: the person who merged the pull request on
        GitHub also moved the ticket to Done on the board. At startup the
        mirror reconcile used to see Done first and walk the ticket
        `merged` on its own, and the pull request was never asked about:
        the run stayed parked with no outcome and no `mergeSha`. GitHub
        is asked before the mirror is repaired, so the run ships."""
        self.parked_on_pr()
        asked = self.fake_client(self.MERGED_PULL)
        provider = StubProvider()
        provider.closed = {"KO-131": "completed"}

        self.main_output(provider=provider)

        self.assertEqual(len(asked), 1)
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("done", "merged", self.MERGE_SHA)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])
        self.assertEqual(self.read('SELECT "action" FROM interventions'
                                   " WHERE action != 'migrate'"),
                         [("approve",)])

    def test_a_github_error_leaves_the_parked_run_for_the_next_pass(self):
        """Review of KO-359: GitHub could not be read at startup while the
        board already said Done. The mirror reconcile used to take that
        Done and walk the ticket `merged` around its parked run, and no
        later pass asked GitHub about a ticket no longer blocked: the run
        was stranded with no outcome and no `mergeSha`. Now the ticket
        stays parked with its run through the failure, and the pass after
        GitHub recovers ships it."""
        self.parked_on_pr()
        asked = self.fake_client(RuntimeError("GitHub is down"),
                                 self.MERGED_PULL)
        provider = StubProvider()
        provider.closed = {"KO-131": "completed"}

        out = self.main_output(provider=provider)

        self.assertEqual(len(asked), 1)
        self.assertIn("could not be read (GitHub is down); the run stays"
                      " parked", out)
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("awaiting_merge_approval", None, None)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("blocked_on_operator",)])
        self.assertEqual(self.read('SELECT COUNT(*) FROM interventions'
                                   " WHERE action != 'migrate'"),
                         [(0,)])

        again = StubProvider()
        again.closed = {"KO-131": "completed"}
        self.main_output(provider=again)

        self.assertEqual(len(asked), 2)
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("done", "merged", self.MERGE_SHA)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])
        self.assertEqual(again.states, [("iss-131", "Done")])
        self.assertEqual(self.read('SELECT "action" FROM interventions'
                                   " WHERE action != 'migrate'"),
                         [("approve",)])

    def test_an_open_pull_request_is_asked_about_once_and_left_alone(self):
        self.parked_on_pr()
        runs = self.read("SELECT * FROM runs")
        tickets = self.read("SELECT * FROM tickets")
        asked = self.fake_client(self.OPEN_PULL)
        provider = StubProvider()

        self.main_output(provider=provider)

        self.assertEqual(len(asked), 1)
        self.assertEqual(self.read("SELECT * FROM runs"), runs)
        self.assertEqual(self.read("SELECT * FROM tickets"), tickets)
        self.assertEqual(provider.states, [])
        self.assertEqual(self.read('SELECT COUNT(*) FROM interventions'
                                   " WHERE action != 'migrate'"),
                         [(0,)])

    # The open pull request as it reads with review activity on it (KO-362):
    # `updatedAt` and the thread count are what the reconcile holds against
    # the run's mark.
    T1, T2, T3 = ("2026-09-10T10:00:00Z", "2026-09-10T11:00:00Z",
                  "2026-09-10T11:00:30Z")

    def open_pull(self, at, threads, checks=None, review=None):
        """The open pull request read at `at` with `threads` review
        threads; `checks` is the head's `statusCheckRollup.state` and
        `review` GitHub's `reviewDecision`, both absent when None (a PR
        with no checks, a repository requiring no review)."""
        pull = dict(self.OPEN_PULL, updatedAt=at,
                    reviewThreads={"totalCount": threads})
        if threads:
            pull["comments"] = {"nodes": [{"id": "comment-1",
                "createdAt": self.T2, "author": {"login": "reviewer"},
                "body": "Please check this"}]}
        if checks is not None:
            pull["commits"] = {"nodes": [
                {"commit": {"statusCheckRollup": {"state": checks}}}]}
        if review is not None:
            pull["reviewDecision"] = review
        return pull

    def parked_with_mark(self, at, threads):
        """A run parked on its pull request whose park recorded `at` and
        `threads` as what it saw, parked long enough ago for
        `[merge] pr_poll_sec` to have passed."""
        self.parked_on_pr()
        self.assertEqual(self.read("SELECT prSeenAt, prSeenThreads FROM"
                                   " runs"), [(None, None)])
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE runs SET prSeenAt = ?, prSeenThreads = ?,"
                         " lastHeartbeat = lastHeartbeat - 200000",
                         (at, threads))
        conn.close()

    def test_new_review_activity_sends_the_parked_run_to_the_babysit(
            self):
        """KO-362: the pull request's `updatedAt` moved past what the last
        pass recorded. The tick sends the run back to the babysitter as
        `--babysit` would -- a `babysit` intervention, the run ended
        with the ticket ready -- and the same pass claims it: the resumed
        run babysits the pull request and parks again, its park recording
        what it saw *after* its own writes (the third answer), so the tick
        after that, reading the same, sends nothing."""
        self.parked_with_mark(self.T1, 0)
        asked = self.fake_client(
            self.open_pull(self.T2, 1, checks="PENDING",
                           review="CHANGES_REQUESTED"),
            self.open_pull(self.T3, 1, checks="SUCCESS", review="APPROVED"))

        out = self.main_output(provider=self.provider())

        self.assertIn(f"KO-131: {self.URL} has new review activity (updated"
                      f" {self.T2}, 1 review threads); run 1 sent back to"
                      " the babysitter", out)
        # Each write records the checks rollup and review decision the
        # same read saw beside the mark (KO-368).
        self.assertEqual(
            self.read("SELECT id, phase, outcome, prSeenAt, prSeenThreads,"
                      " prSeenChecks, prSeenReview FROM runs ORDER BY id"),
            [(1, "failed", "abandoned", self.T2, 1, "pending",
              "changes_requested"),
             (2, "awaiting_merge_approval", None, self.T3, 1, "success",
              "approved")])
        self.assertEqual(
            self.read('SELECT "action", source FROM interventions'
                      " WHERE action != 'migrate'"),
            [("babysit", "supervisor")])
        self.assertEqual(
            self.read("SELECT summary FROM runEvents"
                      " WHERE kind = 'intervention'"),
            [(f"supervisor babysit: new review activity on {self.URL}:"
              " conversation comment;"
              f" updated {self.T2} (last seen {self.T1}), 1 review threads"
              " (last seen 0)",)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("blocked_on_operator",)])
        # Three reads so far: the tick's, the park's after its writes,
        # and the pass after the park's, which saw the same and sent
        # nothing.
        self.assertEqual(len(asked), 3)

        again = self.main_output(provider=StubProvider())

        self.assertEqual(len(asked), 4)
        self.assertNotIn("new review activity", again)
        self.assertEqual(self.read('SELECT COUNT(*) FROM interventions'
                                   " WHERE action != 'migrate'"),
                         [(1,)])
        self.assertEqual(self.read("SELECT phase, prSeenAt FROM runs"
                                   " WHERE id = 2"),
                         [("awaiting_merge_approval", self.T3)])

    def test_an_unchanged_pull_request_is_not_babysat_again(self):
        """Two ticks over the same pull request: the first finds no mark
        on the run (parked by a module older than the columns) and records
        what it saw without babysitting; the second finds the same and
        does nothing. No round, no intervention, the run still parked."""
        self.parked_on_pr()
        asked = self.fake_client(self.open_pull(self.T1, 0, checks="SUCCESS"))

        first = self.main_output(provider=StubProvider())
        second = self.main_output(provider=StubProvider())
        self.assertEqual(len(asked), 2)
        self.assertNotIn("review activity", first + second)
        # No `reviewDecision` in the answer is a null review, not an error.
        self.assertEqual(
            self.read("SELECT phase, outcome, prSeenAt, prSeenThreads,"
                      " prSeenChecks, prSeenReview FROM runs"),
            [("awaiting_merge_approval", None, self.T1, 0, "success", None)])
        self.assertEqual(self.read('SELECT COUNT(*) FROM interventions'
                                   " WHERE action != 'migrate'"),
                         [(0,)])

    def test_an_unchanged_pull_request_still_refreshes_its_facts(self):
        """KO-368 review round 1: the run holds a mark from the last pass
        and facts from the same read (checks pending, review required).
        The pull request has not moved -- same `updatedAt`, same thread
        count -- but its checks went green and a review landed. The tick
        starts no round and leaves the mark alone, yet the facts on the
        run are what the read saw, not what the last pass saw."""
        self.parked_with_mark(self.T1, 0)
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE runs SET prSeenChecks = 'pending',"
                         " prSeenReview = 'review_required'")
        conn.close()
        self.fake_client(self.open_pull(self.T1, 0, checks="SUCCESS",
                                        review="APPROVED"))
        out = self.main_output(provider=StubProvider())
        self.assertNotIn("review activity", out)
        self.assertEqual(
            self.read("SELECT phase, prSeenAt, prSeenThreads, prSeenChecks,"
                      " prSeenReview FROM runs"),
            [("awaiting_merge_approval", self.T1, 0, "success",
              "approved")])
        self.assertEqual(self.read('SELECT COUNT(*) FROM interventions'
                                   " WHERE action != 'migrate'"),
                         [(0,)])

    def test_activity_within_the_poll_interval_waits(self):
        """The pull request moved, but the run parked seconds ago: the tick
        names the activity and the wait rather than starting a round."""
        self.parked_on_pr()
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE runs SET prSeenAt = ?, prSeenThreads = 0",
                         (self.T1,))
        conn.close()
        self.fake_client(self.open_pull(self.T2, 1))
        out = self.main_output(provider=StubProvider())
        self.assertIn(f"KO-131: {self.URL} has new review activity; the next"
                      " babysit round waits", out)
        self.assertIn("([merge] pr_poll_sec)", out)
        self.assertEqual(self.read("SELECT phase, prSeenAt FROM runs"),
                         [("awaiting_merge_approval", self.T1)])
        self.assertEqual(self.read('SELECT COUNT(*) FROM interventions'
                                   " WHERE action != 'migrate'"),
                         [(0,)])

    def test_a_low_github_budget_stops_the_pull_request_reads(self):
        """The read that finds `rateLimit.remaining` under the floor is the
        last one until the reset it names: the activity it saw starts no
        round, the tick prints one line naming `resetAt`, and the next tick
        reads no pull request at all."""
        self.parked_with_mark(self.T1, 0)
        reset = "2999-01-01T00:00:00Z"
        asked = self.fake_client(self.open_pull(self.T2, 1),
                                 rate={"remaining": 200, "resetAt": reset})
        first = self.main_output(provider=self.provider())
        second = self.main_output(provider=self.provider())
        self.assertEqual(len(asked), 1)
        line = ("[holo2] GitHub's GraphQL budget is down to 200 points; no"
                f" parked pull request is read until it resets at {reset}")
        self.assertIn(line, first)
        self.assertIn(line, second)
        self.assertNotIn("sent back to the babysitter", first + second)
        self.assertEqual(self.read("SELECT phase, prSeenAt FROM runs"),
                         [("awaiting_merge_approval", self.T1)])
        self.assertEqual(self.read('SELECT COUNT(*) FROM interventions'
                                   " WHERE action != 'migrate'"),
                         [(0,)])

    def test_the_scheduler_ships_a_merged_pull_request_on_a_timer_tick(
            self):
        """The pool's timer tick asks too: open at startup, merged by the
        tick, and the parked run is closed out between two waits with no
        worker involved (KO-353's tick carrying KO-359's reconcile)."""
        self.parked_on_pr()
        asked = self.fake_client(self.OPEN_PULL, self.MERGED_PULL)
        provider = StubProvider(a_task(2))
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       '[loop]\nworkers = 2\ntick_sec = 30\n')
        pool = FakePool([(TICK, provider.queue.clear),
                         (holophyte.loop.pool.WORKER_MERGED, None)])
        out = io.StringIO()
        with patch.object(holophyte.loop.pool, "SPAWN", pool.spawn), \
                patch.object(holophyte.loop.pool, "WAIT", pool.wait), \
                patch.object(sys, "stdout", out):
            rc = holophyte.cli.operator.main(self.project, provider)
        self.assertEqual(rc, 0)
        self.assertEqual(len(asked), 2)
        self.assertEqual(pool.timeouts, [30, 30])
        self.assertEqual(len(pool.spawned), 1)
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("done", "merged", self.MERGE_SHA)])
        self.assertEqual(
            self.read("SELECT status FROM tickets"
                      " WHERE linearIdentifier = 'KO-131'"),
            [("merged",)])
        self.assertIn(("iss-131", "Done"), provider.states)
        self.assertIn(f"{self.URL} was merged on GitHub", out.getvalue())
