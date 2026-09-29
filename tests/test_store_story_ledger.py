"""A story's witness ledger, parked decisions and closing, kept by the store."""
import unittest

import store
from store.read import ticket_notes
from store.stories import (
    answer_decision,
    approve_story,
    close_story,
    park_story,
    record_witness_result,
    story,
    witness_ledger,
)
from tests.test_store_stories import StoryFixture

LEDGER_TABLES = ("stories", "witnessResults", "storyDecisions",
                 "interventions", "ticketNotes")


class StoryLedgerTests(StoryFixture, unittest.TestCase):
    def approve(self, children=None):
        self.file(children=children)
        approve_story(self.conn, self.parent, 2, "operator", "go")

    def snapshot(self):
        return {table: self.conn.execute(
            f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in LEDGER_TABLES}

    def test_the_ledger_keeps_every_row_and_reads_the_latest_at_a_commit(self):
        self.approve()
        red = record_witness_result(self.conn, self.parent, "W1", "A", "red",
                                    "loop", red_kind="assert", now=1)
        green = record_witness_result(self.conn, self.parent, "W1", "A",
                                      "green", "loop", file_hash="h1",
                                      evidence_path="ev/w1.log", seconds=1.5,
                                      now=2)
        absent = record_witness_result(self.conn, self.parent, "W2", "A",
                                       "absent", "baseline", now=3)
        record_witness_result(self.conn, self.parent, "W1", "B", "error",
                              "operator", now=4)
        self.assertEqual(
            [(row.id, row.witnessKey, row.mainSha, row.verdict, row.redKind)
             for row in witness_ledger(self.conn, self.parent)][:3],
            [(red, "W1", "A", "red", "assert"),
             (green, "W1", "A", "green", None),
             (absent, "W2", "A", "absent", None)])
        self.assertEqual(
            [tuple(row) for row in witness_ledger(self.conn, self.parent, "A")],
            [(green, "W1", "A", "green", None, "loop", "h1", "ev/w1.log", 1.5,
              2),
             (absent, "W2", "A", "absent", None, "baseline", None, None, None,
              3)])

    def test_a_bad_witness_result_is_refused_and_appends_nothing(self):
        self.approve()
        record_witness_result(self.conn, self.parent, "W1", "A", "green",
                              "loop")
        before = self.snapshot()
        cases = [
            ("verdict 'blue'", "W1", "blue", "loop", None),
            ("verifier 'robot'", "W1", "green", "robot", None),
            ("a green verdict has no redKind", "W1", "green", "loop",
             "assert"),
            ("redKind None", "W1", "red", "loop", None),
            ("no witness 'W9'", "W9", "green", "loop", None),
        ]
        for problem, key, verdict, verifier, red_kind in cases:
            with self.subTest(problem=problem):
                with self.assertRaisesRegex(ValueError, problem):
                    record_witness_result(self.conn, self.parent, key, "A",
                                          verdict, verifier,
                                          red_kind=red_kind)
                self.assertEqual(self.snapshot(), before)

    def test_parking_opens_a_decision_the_story_read_lists(self):
        self.approve()
        decision = park_story(self.conn, self.parent, "plan_drift",
                              "KO-4's plan drifted; keep it?",
                              ["keep", "replan"], "keep",
                              ticket_id=self.complete, now=10)
        read = story(self.conn, self.advance)
        self.assertEqual(read.state, "parked")
        self.assertEqual([tuple(open_) for open_ in read.decisions],
                         [(decision, "plan_drift", self.complete,
                           "KO-4's plan drifted; keep it?",
                           ("keep", "replan"), "keep")])

    def test_a_bad_park_is_refused_and_writes_nothing(self):
        self.file()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "is planned, not approved"):
            park_story(self.conn, self.parent, "unmet", "why?", ["a"], "a")
        self.assertEqual(self.snapshot(), before)
        approve_story(self.conn, self.parent, 2, "operator", "go")
        before = self.snapshot()
        cases = [
            ("default 'c' is not among the options", "unmet", "c"),
            ("kind 'stuck'", "stuck", "a"),
        ]
        for problem, kind, default in cases:
            with self.subTest(problem=problem):
                with self.assertRaisesRegex(ValueError, problem):
                    park_story(self.conn, self.parent, kind, "why?",
                               ["a", "b"], default)
                self.assertEqual(self.snapshot(), before)
        for state in ("closed", "abandoned"):
            with self.subTest(state=state):
                with store.transaction(self.conn):
                    self.conn.execute("UPDATE stories SET state = ?"
                                      " WHERE ticketId = ?",
                                      (state, self.parent))
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, f"is {state}, not"):
                    park_story(self.conn, self.parent, "unmet", "why?",
                               ["a"], "a")
                self.assertEqual(self.snapshot(), before)

    def test_the_last_answer_returns_the_story_to_approved(self):
        self.approve()
        first = park_story(self.conn, self.parent, "unmet", "W1 is red",
                           ["retry", "abandon"], "retry")
        second = park_story(self.conn, self.parent, "regressed", "W2 fell",
                            ["revert", "accept"], "revert")
        self.assertEqual(answer_decision(self.conn, first, "abandon",
                                         "operator", "not worth it", now=20),
                         "parked")
        self.assertEqual(self.conn.execute(
            "SELECT answer, answeredBy, answeredAt FROM storyDecisions"
            " WHERE id = ?", (first,)).fetchone(),
            ("abandon", "operator", 20))
        self.assertEqual(self.conn.execute(
            'SELECT projectId, runId, source, "trigger", note, at'
            " FROM interventions WHERE action = 'decide'").fetchall(),
            [(1, None, "human", "manual", "not worth it", 20)])
        read = story(self.conn, self.parent)
        self.assertEqual((read.state, [d.id for d in read.decisions]),
                         ("parked", [second]))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "already answered"):
            answer_decision(self.conn, first, "retry", "operator", "again")
        with self.assertRaisesRegex(ValueError, "'retry' is not an option"):
            answer_decision(self.conn, second, "retry", "operator", "no")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(answer_decision(self.conn, second, "accept",
                                         "operator", "fine"), "approved")
        read = story(self.conn, self.parent)
        self.assertEqual((read.state, read.decisions), ("approved", ()))

    def test_closing_merges_the_parent_and_backlogs_the_ready_child(self):
        self.approve(children=[(self.advance, "advances", ("W1",)),
                               (self.complete, "completes", ("W1", "W2"))])
        with store.transaction(self.conn):
            self.conn.execute("UPDATE tickets SET status = 'merged'"
                              " WHERE id = ?", (self.advance,))
        merged = self.conn.execute("SELECT * FROM tickets WHERE id = ?",
                                   (self.advance,)).fetchone()
        close_story(self.conn, self.parent, "T", "W1 and W2 green at T",
                    now=30)
        read = story(self.conn, self.parent)
        self.assertEqual((read.state, read.closedSha, read.closedAt),
                         ("closed", "T", 30))
        self.assertEqual(self.conn.execute(
            "SELECT status FROM tickets WHERE id = ?",
            (self.parent,)).fetchone(), ("merged",))
        self.assertEqual([note.text for note in
                          ticket_notes(self.conn, self.parent)],
                         ["W1 and W2 green at T"])
        self.assertEqual(self.conn.execute(
            "SELECT status, boardColumn FROM tickets WHERE id = ?",
            (self.complete,)).fetchone(), ("ready", "backlog"))
        self.assertEqual([(note.kind, note.text) for note in
                          ticket_notes(self.conn, self.complete)],
                         [("move", "story closed on its witnesses at T")])
        self.assertEqual(self.conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (self.advance,)).fetchone(),
            merged)

    def test_closing_a_planned_or_closed_story_is_refused(self):
        self.file()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "is planned, not approved"):
            close_story(self.conn, self.parent, "T", "green")
        self.assertEqual(self.snapshot(), before)
        approve_story(self.conn, self.parent, 2, "operator", "go")
        close_story(self.conn, self.parent, "T", "green")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "is closed, not approved"):
            close_story(self.conn, self.parent, "U", "green again")
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
