"""A story's guards: its children wait for its approval, a board Done never
closes it, and canceling its parent abandons it.

Run: python3 -m unittest discover -s tests -p 'test_story_claim_gate.py' -v
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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
import store.stories  # noqa: E402
import store.tickets  # noqa: E402
from holophyte import pr_status  # noqa: E402
from holophyte.native_board import NativeBoard  # noqa: E402
from holophyte.reconcile import _reconcile_mirror  # noqa: E402
from holophyte.runs import open_store  # noqa: E402
from provider import board_for  # noqa: E402

NATIVE = '[board]\nkind = "native"\nkey = "NAT"\n'
WITNESSES = [
    {"key": key, "criterion": f"outcome {key}", "file": f"tests/test_{key}.py",
     "command": f"python3 -m unittest tests.test_{key}",
     "source": f"assert '{key}'\n"} for key in ("W1", "W2")]
PR_URL = "https://github.com/example/repo/pull/7"


def no_linear(*args, **kwargs):
    raise AssertionError("a native project asked Linear")


class StoryClaimLoopTests(LoopFixture):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.configure(NATIVE)
        self.board = board_for(self.project)
        self.assertIsInstance(self.board, NativeBoard)
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, self.board.team, self.project.path)

    def file(self, column):
        identifier = store.board.file_ticket(
            self.conn, self.project_id, "NAT", VALID_BODY, column=column)
        return self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?",
            (identifier,)).fetchone()[0]

    def run_loop(self, message):
        fake = FakeAgent(Commit(message), APPROVE)
        out = io.StringIO()
        with no_agent_processes(), patch.object(sys, "stdout", out), \
                patch.object(holophyte.loop, "agent", fake), \
                patch("holophyte.freshness.critic_admits",
                      return_value=True):
            holophyte.operator.main(self.project, self.board)
        return out.getvalue()

    def runs(self):
        return self.read("SELECT t.linearIdentifier, r.outcome FROM runs r"
                         " JOIN tickets t ON t.id = r.ticketId ORDER BY r.id")

    def test_a_planned_storys_child_waits_until_the_story_is_approved(self):
        parent = self.file("backlog")
        child = self.file("ready")
        others = [self.file("backlog"), self.file("backlog")]
        self.file("ready")
        store.stories.file_story(
            self.conn, parent, WITNESSES,
            [(child, "advances", ("W1",)), (others[0], "advances", ("W2",)),
             (others[1], "completes", ("W1", "W2"))])

        out = self.run_loop("the ordinary work")

        self.assertEqual(self.runs(), [("NAT-5", "merged")], out)
        self.assertEqual(self.read("SELECT COUNT(*) FROM sweepStrikes"), [(0,)])
        skipped = [line for line in out.splitlines() if "NAT-2" in line]
        self.assertTrue(skipped, out)
        self.assertIn("planned", skipped[0])
        self.assertIn("NAT-1", skipped[0])

        (revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?", (parent,)).fetchone()
        store.stories.approve_story(self.conn, parent, revision, "operator",
                                    "the plan holds")
        out = self.run_loop("the child's work")

        self.assertEqual(self.runs(), [("NAT-5", "merged"), ("NAT-2", "merged")],
                         out)
        self.assertIn("the child's work", self.subjects())


class ClosedBoard:
    """A board that holds each named ticket closed in the state given."""

    def __init__(self, closed, states=None):
        self.closed = closed
        self.states = states or {}

    def fetch_task(self, identifier):
        state = self.states.get(identifier)
        return {"board_state": state} if state else None

    def closed_identifiers(self, identifiers):
        return {i: self.closed[i] for i in identifiers if i in self.closed}


class StoryReconcileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(Path(tmp.name) / "store.db")
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(self.conn, "team-1",
                                                       "/repo")
        self.parent = self.mirror(1, contract=False)
        self.ready = self.mirror(3)
        self.running = self.mirror(4)
        store.stories.file_story(
            self.conn, self.parent, WITNESSES,
            [(self.ready, "advances", ("W1", "W2")),
             (self.running, "completes", ("W1", "W2"))])
        store.tickets.transition(self.conn, self.running, "in_flight")
        self.run_id = store.claim(self.conn, self.project_id, self.running)

    def mirror(self, n, contract=True):
        return store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{n}",
            linear_identifier=f"NAT-{n}", title=f"ticket {n}",
            acceptance_criteria=["Given it, then it holds"] if contract else (),
            verification_commands=["echo ok"] if contract else (),
            board_column="ready")

    def reconcile(self, state):
        out = io.StringIO()
        with redirect_stdout(out):
            _reconcile_mirror(self.conn, self.project_id,
                              ClosedBoard({"NAT-1": state}))
        return out.getvalue()

    def value(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()[0]

    def notes(self, ticket_id):
        return self.conn.execute(
            "SELECT kind, text FROM ticketNotes WHERE ticketId = ?",
            (ticket_id,)).fetchall()

    def test_a_board_done_leaves_the_parent_open_with_one_note(self):
        store.stories.record_witness_result(self.conn, self.parent, "W2",
                                            "old", "green", "loop")
        store.stories.record_witness_result(self.conn, self.parent, "W1",
                                            "abc", "green", "loop")
        store.stories.record_witness_result(self.conn, self.parent, "W2",
                                            "abc", "red", "loop",
                                            red_kind="assert")

        self.reconcile("completed")
        out = self.reconcile("completed")

        self.assertEqual(self.value("SELECT status FROM tickets WHERE id = ?",
                                    self.parent), "needs_spec")
        self.assertEqual(self.value("SELECT state FROM stories"), "planned")
        ((_, text),) = self.notes(self.parent)
        self.assertIn("not yet green at abc: W2", text)
        self.assertNotIn("W1", text)
        self.assertIn("NAT-1", out)

    def test_a_done_with_no_ledger_names_every_witness(self):
        self.reconcile("completed")

        ((_, text),) = self.notes(self.parent)
        self.assertIn("W1, W2", text)
        self.assertEqual(self.value("SELECT state FROM stories"), "planned")

    def test_a_board_cancel_abandons_the_story_and_backlogs_idle_children(self):
        self.reconcile("canceled")

        self.assertEqual(self.value("SELECT state FROM stories"), "abandoned")
        self.assertEqual(self.value("SELECT status FROM tickets WHERE id = ?",
                                    self.parent), "abandoned")
        self.assertEqual(self.value(
            "SELECT boardColumn FROM tickets WHERE id = ?", self.ready),
            "backlog")
        self.assertEqual([kind for kind, _ in self.notes(self.ready)], ["move"])
        self.assertEqual(self.conn.execute(
            "SELECT endedAt, outcome FROM runs WHERE id = ?",
            (self.run_id,)).fetchone(), (None, None))
        self.assertEqual(self.value("SELECT status FROM tickets WHERE id = ?",
                                    self.running), "in_flight")
        self.assertEqual(self.value(
            "SELECT boardColumn FROM tickets WHERE id = ?", self.running),
            "ready")

    def cancel_parent_held_on(self, pull):
        for status in ("ready", "in_flight"):
            store.tickets.transition(self.conn, self.parent, status)
        run_id = store.claim(self.conn, self.project_id, self.parent)
        store.park(self.conn, run_id, "awaiting_merge_approval", pr_url=PR_URL)
        store.tickets.transition(self.conn, self.parent, "blocked_on_operator")
        board = ClosedBoard({}, {"NAT-1": "Canceled"})
        with redirect_stdout(io.StringIO()), \
                patch.object(pr_status, "pull_status", return_value=pull), \
                patch("holophyte.board.release_lease_label"):
            _reconcile_mirror(self.conn, self.project_id, board, object())
        return self.value("SELECT outcome FROM runs WHERE id = ?", run_id)

    def assert_story_abandoned(self):
        self.assertEqual(self.value("SELECT state FROM stories"), "abandoned")
        self.assertEqual(self.value("SELECT status FROM tickets WHERE id = ?",
                                    self.parent), "abandoned")
        self.assertEqual(self.value(
            "SELECT boardColumn FROM tickets WHERE id = ?", self.ready),
            "backlog")
        self.assertEqual([kind for kind, _ in self.notes(self.ready)], ["move"])

    def test_a_canceled_parent_parked_on_an_open_pull_request_abandons_it(self):
        outcome = self.cancel_parent_held_on(
            pr_status.PullStatus(merged=False, closed=False))

        self.assertEqual(outcome, "abandoned")
        self.assert_story_abandoned()

    def test_a_canceled_parent_whose_pull_request_closed_abandons_it(self):
        outcome = self.cancel_parent_held_on(
            pr_status.PullStatus(merged=False, closed=True, closed_by="person"))

        self.assertEqual(outcome, "rejected")
        self.assert_story_abandoned()


class NativeCancelTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.conn = store.open(Path(tmp.name) / "store.db")
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(self.conn, "team-1",
                                                       str(repo))
        self.parent = self.file("backlog")
        self.child = self.file("ready")
        store.stories.file_story(self.conn, self.parent, WITNESSES,
                                 [(self.child, "completes", ("W1", "W2"))])

    def file(self, column):
        identifier = store.board.file_ticket(self.conn, self.project_id, "NAT",
                                             VALID_BODY, column=column)
        return self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?",
            (identifier,)).fetchone()[0]

    def rows(self):
        return (self.conn.execute("SELECT state FROM stories").fetchall(),
                self.conn.execute(
                    "SELECT id, status, boardColumn, revision FROM tickets"
                    " ORDER BY id").fetchall())

    def cancel(self):
        (revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?",
            (self.parent,)).fetchone()
        store.board.cancel_ticket(self.conn, self.project_id, "NAT-1", revision,
                                  "The export is no longer wanted.")

    def test_canceling_the_parent_abandons_its_story(self):
        self.cancel()

        self.assertEqual(self.rows()[0], [("abandoned",)])
        self.assertEqual(self.conn.execute(
            "SELECT id, status, boardColumn FROM tickets ORDER BY id").fetchall(),
            [(self.parent, "abandoned", "canceled"),
             (self.child, "ready", "backlog")])

    def test_a_failure_inside_the_cancel_leaves_story_and_parent_unchanged(self):
        before = self.rows()

        with patch.object(store.stories, "record_note",
                          side_effect=RuntimeError("the disk is full")):
            with self.assertRaises(RuntimeError):
                self.cancel()

        self.assertEqual(self.rows(), before)
        self.assertEqual(before[0], [("planned",)])


if __name__ == "__main__":
    unittest.main()
