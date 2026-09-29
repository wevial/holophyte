"""A story child is claimed only from its story's frontier: its approved
dependencies merged, its edges as approved, off a parked decision's branch
and under `[story] max_parallel`.

Run: python3 -m unittest discover -s tests -p 'test_story_frontier.py' -v
"""
from __future__ import annotations

import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import APPROVE, Commit, FakeAgent, no_agent_processes  # noqa: E402
from loop_fixture import VALID_BODY, LoopFixture  # noqa: E402

import holophyte.loop  # noqa: E402
import holophyte.operator  # noqa: E402
import linear_provider  # noqa: E402
import store  # noqa: E402
import store.board  # noqa: E402
import store.tickets  # noqa: E402
from holophyte import story_claim  # noqa: E402
from holophyte.claim_store import claim_from_store, sync_board  # noqa: E402
from holophyte.config import check_config_keys  # noqa: E402
from holophyte.config_tables import story_config  # noqa: E402
from holophyte.native_board import NativeBoard  # noqa: E402
from holophyte.pool import NOTHING_SEEN  # noqa: E402
from holophyte.runs import open_store  # noqa: E402
from provider import board_for  # noqa: E402
from store.stories import (  # noqa: E402
    approve_story,
    file_story,
    park_story,
    story_frontier,
)

NATIVE = '[board]\nkind = "native"\nkey = "NAT"\n'
WITNESS = {"key": "W1", "criterion": "the thing works",
           "file": "tests/test_thing.py",
           "command": "python3 -m unittest tests.test_thing",
           "source": "def test_it_works():\n    pass\n"}
RESTORE = "restore the approved edges"


def no_linear(*args, **kwargs):
    raise AssertionError("a native project asked Linear")


def depending_on(*identifiers):
    return VALID_BODY.replace("Depends on: none",
                              f"Depends on: {', '.join(identifiers)}")


class DropDependencyThenCommit(Commit):
    """A's implementer turn: C's body loses one of its `Depends on:`
    tickets on the board, then A commits its own work."""

    def __init__(self, test, child, kept):
        super().__init__("A's work")
        self.test, self.child, self.kept = test, child, kept

    def play(self, cwd, turn):
        test = self.test
        (revision,) = test.conn.execute(
            "SELECT revision FROM tickets WHERE linearIdentifier = ?",
            (self.child,)).fetchone()
        store.board.edit_ticket(test.conn, test.project_id, self.child,
                                depending_on(*self.kept), revision)
        return super().play(cwd, turn)


class LinearLikeBoard(NativeBoard):
    """The native board's rows served as a Linear store-mode board serves
    them: listed and read back with each ticket's open blockers."""

    native = False
    blocked_by = {}

    def fetch_task(self, issue_id):
        task = super().fetch_task(issue_id)
        return task and dict(task, blocked_by=self.blocked_by.get(issue_id, []))

    def listing(self):
        return [self.fetch_task(issue_id) for issue_id in self.blocked_by]


class FrontierFixture(LoopFixture):
    config = NATIVE

    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.configure(self.config)
        self.board = board_for(self.project)
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, self.board.team, self.project.path)
        self.parent = self.file("backlog")

    def file(self, column, body=VALID_BODY):
        identifier = store.board.file_ticket(
            self.conn, self.project_id, "NAT", body, column=column)
        return self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?",
            (identifier,)).fetchone()[0]

    def approve(self, *children):
        file_story(self.conn, self.parent, [WITNESS],
                   [(child, "advances", ("W1",)) for child in children])
        (revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?",
            (self.parent,)).fetchone()
        approve_story(self.conn, self.parent, revision, "operator", "go")

    def refusal(self, ticket_id):
        with patch.object(sys, "stdout", io.StringIO()):
            return story_claim.refusal(self.project, self.conn, ticket_id)

    def issue(self, ticket_id):
        return self.conn.execute("SELECT linearIssueId FROM tickets"
                                 " WHERE id = ?", (ticket_id,)).fetchone()[0]

    def claim(self, board, sync=False):
        with patch.object(sys, "stdout", io.StringIO()), \
                patch("holophyte.freshness.critic_admits", return_value=True):
            if sync:
                sync_board(self.project, self.conn, self.project_id, board)
            return claim_from_store(self.project, self.conn, self.project_id,
                                    board, "identifier", set(), NOTHING_SEEN)

    def live_runs(self):
        return self.conn.execute(
            "SELECT ticketId FROM runs WHERE endedAt IS NULL").fetchall()

    def open_decisions(self):
        return self.conn.execute(
            "SELECT kind, ticketId, defaultOption FROM storyDecisions"
            " WHERE answer IS NULL").fetchall()

    def story_state(self):
        return self.conn.execute("SELECT state FROM stories").fetchone()[0]


class DriftInTheLoopTests(FrontierFixture):
    def test_a_dependency_dropped_on_the_board_parks_c_and_b_still_merges(self):
        c = self.file("ready")
        a, b = self.file("ready"), self.file("ready")
        store.board.edit_ticket(self.conn, self.project_id, "NAT-2",
                                depending_on("NAT-3", "NAT-4"), 1)
        self.approve(a, b, c)

        fake = FakeAgent(DropDependencyThenCommit(self, "NAT-2", ["NAT-3"]),
                         APPROVE, Commit("B's work"), APPROVE)
        out = io.StringIO()
        with no_agent_processes(), patch.object(sys, "stdout", out), \
                patch.object(holophyte.loop, "agent", fake), \
                patch("holophyte.freshness.critic_admits",
                      return_value=True):
            holophyte.operator.main(self.project, self.board)

        self.assertEqual(self.read(
            "SELECT t.linearIdentifier, r.outcome FROM runs r"
            " JOIN tickets t ON t.id = r.ticketId ORDER BY r.id"),
            [("NAT-3", "merged"), ("NAT-4", "merged")], out.getvalue())
        self.assertEqual(self.open_decisions(), [("plan_drift", c, RESTORE)])
        self.assertEqual(self.story_state(), "parked")
        self.assertIn("B's work", self.subjects())


class FrontierGateTests(FrontierFixture):
    def test_a_parked_decision_refuses_only_its_own_branch(self):
        a = self.file("ready")
        b = self.file("ready")
        c = self.file("ready", depending_on("NAT-2"))
        self.approve(a, b, c)
        decision = park_story(self.conn, self.parent, "unmet",
                              "W1 is not met", ["retry", "abandon"], "retry",
                              ticket_id=a)

        self.assertIsNone(self.refusal(b))
        refused = self.refusal(c)
        self.assertIn(f"decision {decision}", refused)
        self.assertIn("NAT-2", refused)

    def test_a_listing_dropping_an_unmerged_edge_parks_the_story_once(self):
        a, b = self.file("backlog"), self.file("backlog")
        c = self.file("ready", depending_on("NAT-2", "NAT-3"))
        self.approve(a, b, c)
        board = LinearLikeBoard(self.project, self.board.key, self.board.team)
        board.blocked_by = {self.issue(c): [self.issue(a)]}

        answers = [self.claim(board, sync=True) for _ in range(2)]

        self.assertEqual(answers, [(None, None, None)] * 2)
        self.assertEqual(self.conn.execute(
            "SELECT dependsOn FROM tickets WHERE id = ?", (c,)).fetchone(),
            (f'["{self.issue(a)}"]',))
        self.assertEqual(self.open_decisions(), [("plan_drift", c, RESTORE)])
        self.assertEqual(self.story_state(), "parked")
        self.assertEqual(self.live_runs(), [])

    def test_a_read_back_naming_a_blocker_outside_the_plan_parks_the_story(
            self):
        a = self.file("ready")
        store.tickets.walk_ticket(self.conn, a, "merged")
        d = self.file("backlog")
        c = self.file("ready", depending_on("NAT-2"))
        self.approve(a, c)
        board = LinearLikeBoard(self.project, self.board.key, self.board.team)
        board.blocked_by = {self.issue(c): [self.issue(d)]}

        self.assertEqual(self.claim(board), (None, None, None))

        self.assertEqual(self.open_decisions(), [("plan_drift", c, RESTORE)])
        self.assertEqual(self.live_runs(), [])

    def test_a_backlog_child_is_not_on_the_frontier(self):
        a, b = self.file("backlog"), self.file("ready")
        self.approve(a, b)

        self.assertEqual(story_frontier(self.conn, self.parent, 2), ["NAT-3"])


class ParallelCapTests(FrontierFixture):
    config = NATIVE + "[story]\nmax_parallel = 1\n"

    def test_one_child_in_flight_holds_the_other_at_a_cap_of_one(self):
        a, b = self.file("ready"), self.file("ready")
        self.approve(a, b)
        store.tickets.transition(self.conn, a, "in_flight")
        run_id = store.claim(self.conn, self.project_id, a)

        self.assertIn("max_parallel of 1", self.refusal(b))
        self.assertEqual(story_frontier(self.conn, self.parent, 1), [])

        store.release(self.conn, run_id, "failed")

        self.assertEqual(story_frontier(self.conn, self.parent, 1), ["NAT-3"])

    def test_a_sibling_claimed_after_the_last_gate_is_refused_at_the_lease(
            self):
        a, b = self.file("ready"), self.file("ready")
        self.approve(a, b)

        def another_loop_claims_b(*args):
            if not self.live_runs():
                store.claim(self.conn, self.project_id, b)
            return False

        with patch("holophyte.freshness.parked_since_admitted",
                   another_loop_claims_b):
            self.assertEqual(self.claim(self.board), (None, None, None))

        self.assertEqual(self.live_runs(), [(b,)])


class StoryConfigTests(LoopFixture):
    def test_no_story_table_reads_the_defaults(self):
        self.configure("")

        config = story_config(self.project)

        self.assertEqual((config.max_parallel, config.dependency_ready),
                         (2, "merged"))

    def test_a_verified_readiness_and_a_zero_cap_are_refused_by_name(self):
        for line, key in (('dependency_ready = "verified"', "dependency_ready"),
                          ("max_parallel = 0", "max_parallel")):
            with self.subTest(line=line):
                self.configure(f"[story]\n{line}\n")
                check_config_keys(self.project)
                with self.assertRaisesRegex(SystemExit,
                                            rf"\[story\] {key} must be"):
                    story_config(self.project)


if __name__ == "__main__":
    unittest.main()
