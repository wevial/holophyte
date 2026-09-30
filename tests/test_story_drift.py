"""A story child whose siblings merged while it ran, at the local merge gate.

Run: python3 -m unittest discover -s tests -p 'test_story_drift.py' -v
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import (  # noqa: E402
    APPROVE,
    MALFORMED,
    REQUEST_CHANGES,
    Commit,
    FakeAgent,
    no_agent_processes,
)
from loop_fixture import VALID_BODY, LoopFixture  # noqa: E402

import holophyte.cli.operator  # noqa: E402
import holophyte.loop.adjudicate  # noqa: E402
import holophyte.loop.implement  # noqa: E402
import holophyte.loop.merge_gate  # noqa: E402
import holophyte.loop.review_round  # noqa: E402
import linear_provider  # noqa: E402
import store  # noqa: E402
import store.board  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from provider import board_for  # noqa: E402
from store.stories import approve_story, file_story  # noqa: E402

NATIVE = '[board]\nkind = "native"\nkey = "NAT"\n'
SHARED = "one\ntwo\nthree\nfour\nfive\n"
WITNESS = {"key": "W1", "criterion": "the thing works",
           "file": "tests/test_thing.py",
           "command": "python3 -m unittest tests.test_thing",
           "source": "def test_it_works():\n    pass\n"}


def no_linear(*args, **kwargs):
    raise AssertionError("a native project asked Linear")


class SiblingThenCommit(Commit):
    """B's implementer turn: sibling A's change lands on main and A merges
    first, then B commits its own work."""

    def __init__(self, test, sibling_path, sibling_body, **kwargs):
        super().__init__("the child's work", **kwargs)
        self.test, self.sibling = test, (sibling_path, sibling_body)

    def play(self, cwd, turn):
        path, body = self.sibling
        (self.test.target / path).write_text(body)
        self.test.git("add", "-A")
        self.test.git("commit", "-q", "-m", "the sibling's work")
        store.tickets.walk_ticket(self.test.conn, self.test.a_id, "merged")
        return super().play(cwd, turn)


class SiblingMergesThenCommit(Commit):
    """B's implementer turn: sibling A's run lands its change on main through
    the local merge, and its ticket close-out has not run yet."""

    def __init__(self, test, **kwargs):
        super().__init__("the child's work", **kwargs)
        self.test = test

    def play(self, cwd, turn):
        test = self.test
        a_run = store.claim(test.conn, test.project_id, test.a_id)
        store.set_phase(test.conn, a_run, "merge_gate", "the sibling's gate")
        wt = test.target.parent / "sibling-wt"
        test.git("worktree", "add", "-q", "-b", "sibling", str(wt), "main")
        (wt / "shared.txt").write_text(SHARED.replace("one", "ONE"))
        test.git("commit", "-q", "-am", "the sibling's work", cwd=wt)
        sha = test.git("rev-parse", "HEAD", cwd=wt).strip()
        holophyte.loop.merge_gate._merge(test.project, test.conn, a_run, test.board,
                                    "NAT-3", "the sibling's work", "sibling",
                                    wt, sha)
        return super().play(cwd, turn)


class StoryDriftTests(LoopFixture):
    def setUp(self):
        super().setUp()
        (self.target / "shared.txt").write_text(SHARED)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "a shared file")
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.configure(NATIVE)
        self.board = board_for(self.project)
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = project_id = store.tickets.ensure_project(
            self.conn, self.board.team, self.project.path)
        self.parent = parent = self.file(project_id, "backlog")
        self.b_id, self.a_id = (self.file(project_id, "ready")
                                for _ in range(2))
        file_story(self.conn, parent, [WITNESS],
                   [(self.a_id, "advances", ("W1",)),
                    (self.b_id, "completes", ("W1",))])
        approve_story(self.conn, parent, 1, "operator", "go")

    def file(self, project_id, column):
        identifier = store.board.file_ticket(self.conn, project_id, "NAT",
                                             VALID_BODY, column=column)
        (ticket_id,) = self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?",
            (identifier,)).fetchone()
        return ticket_id

    def run_loop(self, *script):
        fake = FakeAgent(*script)
        self.review_rounds = []

        def agent(*args, review_round=None, **kwargs):
            self.review_rounds.append(review_round)
            return fake(*args, review_round=review_round, **kwargs)

        out = io.StringIO()
        with no_agent_processes(), patch.object(sys, "stdout", out), \
                patch.object(holophyte.loop.implement, "agent", agent), \
                patch.object(holophyte.loop.review_round, "agent", agent), \
                patch.object(holophyte.loop.adjudicate, "agent", agent), \
                patch("holophyte.review.freshness.critic_admits",
                      return_value=True):
            holophyte.cli.operator.main(self.project, self.board)
        return fake, out.getvalue()

    def b_run(self):
        ((run_id,),) = self.read(
            f"SELECT id FROM runs WHERE ticketId = {self.b_id}")
        return run_id

    def drift_events(self):
        return [summary for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'story_drift'"
            f" AND runId = {self.b_run()}")]

    def rounds(self):
        return self.read("SELECT round, verdict FROM reviewRounds"
                         f" WHERE runId = {self.b_run()} ORDER BY round")

    def status(self, ticket_id):
        return self.read("SELECT status, blockedQuestion FROM tickets"
                         f" WHERE id = {ticket_id}")[0]

    def test_a_sibling_touching_other_files_is_recorded_and_b_merges(self):
        _, out = self.run_loop(
            SiblingThenCommit(self, "sibling.txt", "the sibling\n"), APPROVE)

        (event,) = self.drift_events()
        self.assertIn("generation 0", event, out)
        self.assertIn("1 at the merge gate", event)
        self.assertIn("no shared files", event)
        self.assertEqual(self.rounds(), [(1, "pass")])
        self.assertEqual(self.status(self.b_id)[0], "merged", out)
        self.assertIn("the child's work", self.subjects())

    def test_a_shared_file_gets_one_covering_review_before_b_merges(self):
        fake, out = self.run_loop(
            SiblingThenCommit(self, "shared.txt", SHARED.replace("one", "ONE"),
                              path="shared.txt",
                              body=SHARED.replace("five", "FIVE")),
            APPROVE, APPROVE)

        self.assertEqual(self.status(self.b_id)[0], "merged", out)
        refreshed = self.git("rev-parse", "main^2").strip()
        reviewed = self.git("rev-parse", "main^2^1").strip()
        self.assertEqual(self.git("log", "-1", "--format=%s", reviewed).strip(),
                         "the child's work")
        self.assertEqual(self.rounds(), [(1, "pass"), (2, "pass")])
        (event,) = self.drift_events()
        self.assertIn("shared files: shared.txt", event)
        self.assertEqual(fake.roles, ["implement", "review", "review"])
        self.assertEqual(self.review_rounds, [None, 1, 2])
        goal = fake.turns[-1].goal
        self.assertIn(reviewed, goal)
        self.assertIn(refreshed, goal)
        self.assertIn("Sibling tickets of the same story merged into main", goal)
        self.assertIn("(shared.txt)", goal)
        self.assertNotIn("review threads", goal)
        self.assertLess(out.index("verify ok before merge"),
                        out.index("refreshed candidate"))
        self.assertEqual((self.target / "shared.txt").read_text(),
                         SHARED.replace("one", "ONE").replace("five", "FIVE"))

    def test_changes_requested_at_the_covering_review_park_b_unmerged(self):
        _, out = self.run_loop(
            SiblingThenCommit(self, "shared.txt", SHARED.replace("one", "ONE"),
                              path="shared.txt",
                              body=SHARED.replace("five", "FIVE")),
            APPROVE, REQUEST_CHANGES)

        status, question = self.status(self.b_id)
        self.assertEqual(status, "blocked_on_operator", out)
        self.assertIn("shared.txt", question)
        self.assertEqual(self.rounds(), [(1, "pass"), (2, "changes_requested")])
        self.assertNotIn("the child's work", self.subjects())
        self.assertNotIn("FIVE", self.git("show", "main:shared.txt"))

    def test_a_sibling_merged_just_before_b_gets_b_the_covering_review(self):
        fake, out = self.run_loop(
            SiblingMergesThenCommit(self, path="shared.txt",
                                    body=SHARED.replace("five", "FIVE")),
            APPROVE, APPROVE)

        (event,) = self.drift_events()
        self.assertIn("shared files: shared.txt", event, out)
        self.assertEqual(fake.roles, ["implement", "review", "review"])
        self.assertIn("Merge sibling: the sibling's work", self.subjects())
        self.assertEqual(self.status(self.b_id)[0], "merged", out)
        store.tickets.walk_ticket(self.conn, self.a_id, "merged")
        self.assertEqual(self.read("SELECT generation FROM stories WHERE"
                                   f" ticketId = {self.parent}"), [(2,)])

    def test_a_covering_review_with_no_verdict_is_a_reviewer_route_failure(self):
        _, out = self.run_loop(
            SiblingThenCommit(self, "shared.txt", SHARED.replace("one", "ONE"),
                              path="shared.txt",
                              body=SHARED.replace("five", "FIVE")),
            APPROVE, MALFORMED, MALFORMED)

        self.assertEqual(
            self.read("SELECT failureKind, outcomeClass FROM runs"
                      f" WHERE id = {self.b_run()}"),
            [("review_route", "infra")], out)
        self.assertNotEqual(self.status(self.b_id)[0], "blocked_on_operator")
        self.assertNotIn("the child's work", self.subjects())
