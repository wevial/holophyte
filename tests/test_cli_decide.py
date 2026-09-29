"""`--decide KEY-n ID [OPTION] --note TEXT` answers a parked story's decision
with a `decide` intervention and applies the option chosen, against a real
store and a real git repository.

Run: python3 -m unittest discover -s tests -p 'test_cli_decide.py' -v
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loop_fixture import VALID_BODY  # noqa: E402
from test_story_close import (  # noqa: E402
    W1_FILE,
    StoryCloseFixture,
    witness,
)

import holophyte.cli  # noqa: E402
import store.board  # noqa: E402
import store.tickets  # noqa: E402
from holophyte import story_claim  # noqa: E402
from store.stories import approve_story, file_story, story  # noqa: E402
from tests.test_story_frontier import depending_on  # noqa: E402
from tests.test_witness_runner import FAILS_AN_ASSERTION, PASSES  # noqa: E402

DECIDES = "SELECT note FROM interventions WHERE action = 'decide'"
ANSWERS = "SELECT answer FROM storyDecisions ORDER BY id"


class DecideFixture(StoryCloseFixture):
    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                status = holophyte.cli.cli([str(self.target), *args]) or 0
            except SystemExit as exited:
                status = exited.code
        return status, out.getvalue()

    def parked_unmet(self, text):
        parent, child = self.tickets()
        self.approve(parent, child, [witness("W1", W1_FILE)])
        store.tickets.walk_ticket(self.conn, child, "merged")
        self.commit(W1_FILE, text, "w1 lands")
        self.step()
        (decision,) = story(self.conn, parent).decisions
        self.assertEqual(decision.kind, "unmet")
        return parent, decision.id


class DecideTests(DecideFixture):
    def test_the_default_answer_records_the_note_and_approves_the_story(self):
        parent, decision = self.parked_unmet(FAILS_AN_ASSERTION)

        status, out = self.cli("--decide", "NAT-1", str(decision),
                               "--note", "dedupe on the payment id")

        self.assertEqual(status, 0, out)
        self.assertEqual(self.read(ANSWERS), [("file a follow-up child",)])
        self.assertEqual(self.read(DECIDES), [("dedupe on the payment id",)])
        self.assertEqual(story(self.conn, parent).state, "approved")

    def test_accepting_the_changed_file_lets_the_next_pass_close(self):
        edited = PASSES + "# edited\n"
        parent, decision = self.parked_unmet(edited)

        status, out = self.cli("--decide", "NAT-1", str(decision), "2",
                               "--note", "ok")

        self.assertEqual(status, 0, out)
        (w1,) = story(self.conn, parent).witnesses
        self.assertEqual(
            (w1.source, w1.sourceHash),
            (self.git("show", f"main:{W1_FILE}"),
             hashlib.sha256(edited.encode()).hexdigest()))
        status, out = self.cli("--witness-pass", "NAT-1")
        self.assertEqual(status, 0, out)
        found = story(self.conn, parent)
        self.assertEqual((found.state, found.closedSha), ("closed", self.tip()))

    def test_re_approving_a_drifted_plan_lets_the_child_through(self):
        parent = self.tickets()[0]
        b = self.ticket_id(store.board.file_ticket(
            self.conn, self.project_id, "NAT", VALID_BODY, column="ready"))
        c = self.ticket_id(store.board.file_ticket(
            self.conn, self.project_id, "NAT", depending_on("NAT-3"),
            column="ready"))
        file_story(self.conn, parent, [witness("W1", W1_FILE)],
                   [(b, "advances", ["W1"]), (c, "completes", ["W1"])])
        (revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?", (parent,)).fetchone()
        approve_story(self.conn, parent, revision, "operator", "go")
        (c_revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?", (c,)).fetchone()
        store.board.edit_ticket(self.conn, self.project_id, "NAT-4",
                                VALID_BODY, c_revision)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNotNone(story_claim.refusal(self.project, self.conn, c))
        (decision,) = story(self.conn, parent).decisions
        self.assertEqual(decision.kind, "plan_drift")

        status, out = self.cli("--decide", "NAT-1", str(decision.id), "2",
                               "--note", "B is not needed")

        self.assertEqual(status, 0, out)
        found = story(self.conn, parent)
        self.assertEqual(found.approvedPlan["edges"]["NAT-4"], [])
        self.assertEqual(found.state, "approved")
        self.assertIsNone(story_claim.refusal(self.project, self.conn, c))

    def test_abandoning_moves_the_unclaimed_children_to_backlog(self):
        parent, done = self.tickets()
        later = self.ticket_id(store.board.file_ticket(
            self.conn, self.project_id, "NAT", VALID_BODY, column="backlog"))
        file_story(self.conn, parent, [witness("W1", W1_FILE)],
                   [(done, "completes", ["W1"]), (later, "scaffolding", [])])
        (revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?", (parent,)).fetchone()
        approve_story(self.conn, parent, revision, "operator", "go")
        store.tickets.walk_ticket(self.conn, done, "merged")
        self.commit(W1_FILE, FAILS_AN_ASSERTION, "w1 lands red")
        self.step()
        (decision,) = story(self.conn, parent).decisions
        (later_revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?", (later,)).fetchone()
        store.board.move_ticket(self.conn, self.project_id, "NAT-3", "ready",
                                later_revision)

        status, out = self.cli("--decide", "NAT-1", str(decision.id), "4",
                               "--note", "not worth it")

        self.assertEqual(status, 0, out)
        self.assertEqual(story(self.conn, parent).state, "abandoned")
        self.assertEqual(self.read(
            f"SELECT status FROM tickets WHERE id = {parent}"),
            [("abandoned",)])
        self.assertEqual(self.read(
            f"SELECT boardColumn FROM tickets WHERE id = {later}"),
            [("backlog",)])

    def test_amending_the_witness_returns_the_story_to_planned(self):
        parent, decision = self.parked_unmet(FAILS_AN_ASSERTION)

        status, out = self.cli("--decide", "NAT-1", str(decision), "3",
                               "--note", "the witness asks too much")

        self.assertEqual(status, 0, out)
        found = story(self.conn, parent)
        self.assertEqual((found.state, [w.key for w in found.witnesses]),
                         ("planned", ["W1"]))

    def test_a_bad_answer_exits_1_and_writes_nothing(self):
        parent, decision = self.parked_unmet(FAILS_AN_ASSERTION)

        for args, problem in (
                ((str(decision), "9"), f"decision {decision} has 4 options"),
                ((str(decision + 1),), f"holds no decision {decision + 1}")):
            status, out = self.cli("--decide", "NAT-1", *args, "--note", "x")
            self.assertEqual(status, 1, out)
            self.assertIn(problem, out)
        self.assertEqual((self.read(ANSWERS), self.read(DECIDES)),
                         ([(None,)], []))

        self.assertEqual(self.cli("--decide", "NAT-1", str(decision),
                                  "--note", "first")[0], 0)
        status, out = self.cli("--decide", "NAT-1", str(decision), "4",
                               "--note", "second")

        self.assertEqual(status, 1, out)
        self.assertIn(f"decision {decision} is already answered", out)
        self.assertEqual((self.read(ANSWERS), self.read(DECIDES)),
                         ([("file a follow-up child",)], [("first",)]))
        self.assertEqual(story(self.conn, parent).state, "approved")


if __name__ == "__main__":
    import unittest
    unittest.main()
