"""`--decide KEY-n pN [OPTION] --note TEXT` answers a story's proposed child:
accepting files it in Backlog as a scaffolding child depending on the child
that raised it and adds it to the approved plan, rejecting files nothing.
Runs against a real store and a real git repository.

Run: python3 -m unittest discover -s tests -p 'test_story_proposal_decide.py' -v
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loop_fixture import VALID_BODY  # noqa: E402
from test_cli_decide import DECIDES, DecideFixture  # noqa: E402
from test_store_claim_loop import STORE_MODE  # noqa: E402
from test_story_close import (  # noqa: E402
    PROPOSALS,
    PROPOSED,
    W1_FILE,
    witness,
)
from test_story_linear_filing import RecordingBoard  # noqa: E402

import linear_provider  # noqa: E402
import provider  # noqa: E402
import store.board  # noqa: E402
import store.tickets  # noqa: E402
import ticket_template  # noqa: E402
from holophyte.board.native_board import NativeBoard  # noqa: E402
from holophyte.loop.follow_ups import draft_title  # noqa: E402
from holophyte.story import story_claim  # noqa: E402
from store.stories import (  # noqa: E402
    approve_story,
    close_story,
    file_story,
    story,
    story_frontier,
)
from tests.test_story_frontier import depending_on  # noqa: E402
from tests.test_witness_runner import FAILS_AN_ASSERTION  # noqa: E402

ACCEPTED_CHILD = {"identifier": "NAT-3", "role": "scaffolding",
                  "witnessKeys": []}


class ProposalDecideFixture(DecideFixture):
    def proposed(self):
        """NAT-1 approved, its child NAT-2 merged, and NAT-2's proposal p1."""
        parent, child = self.tickets()
        self.approve(parent, child, [witness("W1", W1_FILE)])
        self.propose(child)
        return parent, child

    def rows(self):
        return [self.read(f"SELECT * FROM {table} ORDER BY rowid")
                for table in ("storyProposals", "storyChildren", "tickets",
                              "interventions")]

    def children(self, parent):
        return self.read("SELECT ticketId, witnessKey, role FROM storyChildren"
                         f" WHERE storyId = {parent} ORDER BY ticketId")


class AcceptTests(ProposalDecideFixture):
    def test_accepting_files_a_backlog_scaffolding_child_in_the_plan(self):
        parent, child = self.proposed()
        before = story(self.conn, parent)

        status, out = self.cli("--decide", "NAT-1", "p1",
                               "--note", "belongs in W2")

        self.assertEqual(status, 0, out)
        self.assertEqual(out.splitlines()[-3:], [
            "[holo2] proposal p1 of story NAT-1: accept the proposed child",
            "[holo2] child NAT-3 filed in Backlog",
            "[holo2] story NAT-1 is approved"])
        new = self.ticket_id("NAT-3")
        (body, column, parent_id), = self.read(
            "SELECT body, boardColumn, parentTicketId FROM tickets"
            f" WHERE id = {new}")
        self.assertEqual((column, parent_id), ("backlog", parent))
        self.assertEqual(ticket_template.parse(body).depends_on, ["NAT-2"])
        self.assertIn("## Story\n\nRole: scaffolding\n", body)
        self.assertEqual(self.children(parent), [(child, "W1", "completes"),
                                                 (new, "", "scaffolding")])
        after = story(self.conn, parent)
        self.assertEqual(after.approvedPlan["children"],
                         [*before.approvedPlan["children"], ACCEPTED_CHILD])
        self.assertEqual(after.approvedPlan["edges"]["NAT-3"], ["NAT-2"])
        self.assertEqual((after.state, after.generation),
                         (before.state, before.generation))
        self.assertEqual(self.read(
            "SELECT state, childTicketId FROM storyProposals"),
            [("accepted", new)])
        self.assertEqual(self.read(DECIDES), [("belongs in W2",)])

    def test_the_accepted_child_joins_the_frontier_once_moved_to_ready(self):
        parent, _child = self.proposed()
        self.assertEqual(self.cli("--decide", "NAT-1", "p1", "--note",
                                  "belongs in W2")[0], 0)
        new = self.ticket_id("NAT-3")

        self.assertEqual(story_frontier(self.conn, parent, 2), [])

        (revision,) = self.read(
            f"SELECT revision FROM tickets WHERE id = {new}")[0]
        revision = store.board.edit_ticket(self.conn, self.project_id,
                                           "NAT-3", depending_on("NAT-2"),
                                           revision)
        store.board.move_ticket(self.conn, self.project_id, "NAT-3", "ready",
                                revision)

        self.assertEqual(story_frontier(self.conn, parent, 2), ["NAT-3"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(story_claim.refusal(self.project, self.conn,
                                                  new))
        self.assertEqual(story(self.conn, parent).decisions, ())

    def test_a_board_refusal_exits_1_and_writes_nothing(self):
        self.proposed()
        before = self.rows()

        with patch.object(NativeBoard, "file",
                          side_effect=RuntimeError("the board is down")):
            status, out = self.cli("--decide", "NAT-1", "p1", "--note", "x")

        self.assertEqual(status, 1, out)
        self.assertIn("the board is down", out)
        self.assertEqual(self.read(PROPOSALS), [("proposed",)])
        self.assertEqual(self.rows(), before)


class RejectAndRefuseTests(ProposalDecideFixture):
    def test_rejecting_files_nothing_and_leaves_the_plan(self):
        parent, _child = self.proposed()
        tickets, children = self.read("SELECT * FROM tickets"), self.children(
            parent)
        plan = story(self.conn, parent).approvedPlan

        status, out = self.cli("--decide", "NAT-1", "p1", "2",
                               "--note", "out of story")

        self.assertEqual(status, 0, out)
        self.assertEqual(self.read(
            "SELECT state, childTicketId, decidedBy FROM storyProposals"),
            [("rejected", None, "cli")])
        self.assertEqual(self.read("SELECT * FROM tickets"), tickets)
        self.assertEqual(self.children(parent), children)
        self.assertEqual(story(self.conn, parent).approvedPlan, plan)
        self.assertEqual(self.read(DECIDES), [("out of story",)])

    def test_a_proposal_the_answer_cannot_reach_exits_1_and_writes_nothing(
            self):
        parent, _child = self.proposed()
        before = self.rows()
        for args, problem in (
                (("p1", "3"), "proposal p1 has 2 options; there is no option 3"),
                (("p9",), "story NAT-1 holds no proposal p9")):
            status, out = self.cli("--decide", "NAT-1", *args, "--note", "x")
            self.assertEqual(status, 1, out)
            self.assertIn(problem, out)
            self.assertEqual(self.rows(), before)

        self.assertEqual(self.cli("--decide", "NAT-1", "p1", "2",
                                  "--note", "no")[0], 0)
        before = self.rows()
        status, out = self.cli("--decide", "NAT-1", "p1", "--note", "x")
        self.assertEqual(status, 1, out)
        self.assertIn("proposal p1 is already rejected", out)
        self.assertEqual(self.rows(), before)

        close_story(self.conn, parent, self.tip(), "closed")
        before = self.rows()
        status, out = self.cli("--decide", "NAT-1", "p1", "--note", "x")
        self.assertEqual(status, 1, out)
        self.assertIn("story NAT-1 is closed", out)
        self.assertEqual(self.rows(), before)

    def test_an_open_proposal_neither_parks_the_story_nor_gates_its_frontier(
            self):
        parent, done = self.tickets()
        later = self.ticket_id(store.board.file_ticket(
            self.conn, self.project_id, "NAT", VALID_BODY,
            column="backlog"))
        file_story(self.conn, parent, [witness("W1", W1_FILE)],
                   [(done, "completes", ["W1"]), (later, "scaffolding", [])])
        (revision,) = self.read(
            f"SELECT revision FROM tickets WHERE id = {parent}")[0]
        approve_story(self.conn, parent, revision, "operator", "go")
        self.propose(done)
        self.commit(W1_FILE, FAILS_AN_ASSERTION, "w1 lands red")
        self.step()
        (decision,) = story(self.conn, parent).decisions

        status, out = self.cli("--decide", "NAT-1", str(decision.id),
                               "--note", "a child follows")

        self.assertEqual(status, 0, out)
        self.assertEqual(story(self.conn, parent).state, "approved")
        self.assertEqual(self.read(PROPOSALS), [("proposed",)])
        (later_revision,) = self.read(
            f"SELECT revision FROM tickets WHERE id = {later}")[0]
        store.board.move_ticket(self.conn, self.project_id, "NAT-3", "ready",
                                later_revision)
        self.assertEqual(story_frontier(self.conn, parent, 2), ["NAT-3"])


class LinearAcceptTests(ProposalDecideFixture):
    config = STORE_MODE

    def tickets(self):
        return [store.tickets.mirror_ticket(
            self.conn, self.project_id, f"issue-{n}", f"KO-{n}", f"KO-{n}",
            board_state="Todo", board_column="ready") for n in (1, 2)]

    def test_the_child_is_a_blocked_sub_issue_mirrored_into_the_store(self):
        parent, child = self.proposed()
        board = RecordingBoard("project-1", "team-1", store_mode=True)
        board.issues.update({"KO-1": {"id": "issue-1"},
                             "KO-2": {"id": "issue-2"}})

        with patch.object(provider, "LinearBoard", lambda *a, **k: board), \
                patch.object(linear_provider, "_gql", board.answer):
            status, out = self.cli("--decide", "KO-1", "p1", "--note", "yes")

        self.assertEqual(status, 0, out)
        self.assertEqual(board.calls, [("file", draft_title(PROPOSED),
                                        "Backlog", "issue-1", ["KO-2"])])
        new = self.ticket_id("REL-3")
        self.assertEqual(self.read(
            "SELECT linearIssueId, boardColumn, parentTicketId, dependsOn"
            f" FROM tickets WHERE id = {new}"),
            [("issue-3", "backlog", parent, json.dumps(["issue-2"]))])
        self.assertEqual(self.children(parent), [(child, "W1", "completes"),
                                                 (new, "", "scaffolding")])
        self.assertEqual(story(self.conn, parent).approvedPlan["edges"]["REL-3"],
                         ["issue-2"])
        self.assertEqual(self.read(
            "SELECT state, childTicketId FROM storyProposals"),
            [("accepted", new)])


if __name__ == "__main__":
    import unittest
    unittest.main()
