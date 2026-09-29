"""A story's rows: filed, read back, approved and abandoned by the store."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import store
from store.read import ticket_notes
from store.stories import abandon_story, approve_story, file_story, story

WITNESSES = [
    {"key": "W1", "criterion": "orders export", "file": "tests/test_w1.py",
     "command": "python3 -m unittest tests.test_w1", "source": "assert 1\n"},
    {"key": "W2", "criterion": "one file", "file": "tests/test_w2.py",
     "command": "python3 -m unittest tests.test_w2", "source": "assert 2\n"},
]
STORY_TABLES = ("stories", "storyWitnesses", "storyChildren")


class StoryStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(Path(tmp.name) / "store.db")
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO projects (linearTeamId, repoPath,"
                          " defaultBranch, autonomyProfile)"
                          " VALUES ('t', '/repo', 'main', 'personal')")
        self.parent = self.ticket("KO-1", revision=2)
        self.scaffold = self.ticket("KO-2")
        self.advance = self.ticket("KO-3")
        self.complete = self.ticket("KO-4", depends_on=["KO-2", "KO-3"])
        self.conn.commit()

    def ticket(self, identifier, status="ready", revision=1, depends_on=(),
               project_id=1):
        return self.conn.execute(
            "INSERT INTO tickets (projectId, linearIssueId, linearIdentifier,"
            " title, mirroredAt, status, affinity, revision, boardColumn,"
            " boardState, dependsOn)"
            " VALUES (?, ?, ?, 't', 1, ?, 'any', ?, 'ready', 'Ready', ?)",
            (project_id, identifier, identifier, status, revision,
             json.dumps(list(depends_on)))).lastrowid

    def children(self):
        return [(self.scaffold, "scaffolding", ()),
                (self.advance, "advances", ("W1",)),
                (self.complete, "completes", ("W1", "W2"))]

    def file(self, parent=None, children=None):
        file_story(self.conn, parent or self.parent, WITNESSES,
                   self.children() if children is None else children,
                   standing_orders=["Keep the CSV header stable."])

    def snapshot(self):
        tables = {table: self.conn.execute(
            f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            for table in STORY_TABLES}
        tables["parents"] = self.conn.execute(
            "SELECT id, parentTicketId FROM tickets ORDER BY id").fetchall()
        return tables

    def test_filing_writes_the_story_its_witnesses_and_its_children(self):
        self.file()
        self.assertEqual(self.conn.execute(
            "SELECT ticketId, state, generation, standingOrders FROM stories"
        ).fetchall(), [(self.parent, "planned", 0,
                        '["Keep the CSV header stable."]')])
        self.assertEqual(self.conn.execute(
            "SELECT key, sourceHash, completedBy FROM storyWitnesses"
            " ORDER BY key").fetchall(),
            [("W1", hashlib.sha256(b"assert 1\n").hexdigest(), self.complete),
             ("W2", hashlib.sha256(b"assert 2\n").hexdigest(), self.complete)])
        self.assertEqual(self.conn.execute(
            "SELECT ticketId, witnessKey, role FROM storyChildren"
            " ORDER BY ticketId, witnessKey").fetchall(),
            [(self.scaffold, "", "scaffolding"),
             (self.advance, "W1", "advances"),
             (self.complete, "W1", "completes"),
             (self.complete, "W2", "completes")])
        self.assertEqual(self.conn.execute(
            "SELECT id, parentTicketId FROM tickets ORDER BY id").fetchall(),
            [(self.parent, None), (self.scaffold, self.parent),
             (self.advance, self.parent), (self.complete, self.parent)])

    def test_a_bad_filing_is_refused_and_writes_nothing(self):
        self.file()
        other = self.ticket("KO-5")
        spare = self.ticket("KO-6")
        self.conn.commit()
        before = self.snapshot()
        duplicate = [WITNESSES[0], dict(WITNESSES[1], key="W1")]
        cases = [
            ("already has a story", self.parent, WITNESSES,
             [(spare, "scaffolding", ())]),
            ("is the parent", other, WITNESSES, [(other, "scaffolding", ())]),
            ("'owns'", other, WITNESSES, [(spare, "owns", ("W1",))]),
            ("W9 names no witness", other, WITNESSES,
             [(spare, "advances", ("W9",))]),
            ("W1 is given twice", other, duplicate,
             [(spare, "completes", ("W1",))]),
            ("999 is not in the store", other, WITNESSES,
             [(spare, "scaffolding", ()), (999, "completes", ("W1",))]),
        ]
        for problem, parent, witnesses, children in cases:
            with self.subTest(problem=problem):
                with self.assertRaisesRegex(ValueError, problem):
                    file_story(self.conn, parent, witnesses, children)
                self.assertEqual(self.snapshot(), before)

    def test_story_reads_from_the_parent_or_a_child(self):
        self.file()
        unrelated = self.ticket("KO-5")
        self.conn.commit()
        from_parent = story(self.conn, self.parent)
        self.assertEqual(from_parent, story(self.conn, self.advance))
        self.assertEqual(from_parent.ticketId, self.parent)
        self.assertEqual(from_parent.state, "planned")
        self.assertEqual([w.key for w in from_parent.witnesses], ["W1", "W2"])
        self.assertEqual([(c.ticketId, c.witnessKey, c.role)
                          for c in from_parent.children],
                         [(self.scaffold, "", "scaffolding"),
                          (self.advance, "W1", "advances"),
                          (self.complete, "W1", "completes"),
                          (self.complete, "W2", "completes")])
        self.assertIsNone(story(self.conn, unrelated))

    def test_approval_freezes_the_plan_and_records_the_intervention(self):
        self.file()
        intervention = approve_story(self.conn, self.parent, 2, "operator",
                                     "the plan reads right", now=50)
        read = story(self.conn, self.parent)
        self.assertEqual((read.state, read.approvedRevision, read.approvedBy,
                          read.approvedAt), ("approved", 2, "operator", 50))
        self.assertEqual(read.approvedPlan, {
            "children": [
                {"identifier": "KO-2", "role": "scaffolding",
                 "witnessKeys": []},
                {"identifier": "KO-3", "role": "advances",
                 "witnessKeys": ["W1"]},
                {"identifier": "KO-4", "role": "completes",
                 "witnessKeys": ["W1", "W2"]}],
            "edges": {"KO-2": [], "KO-3": [], "KO-4": ["KO-2", "KO-3"]}})
        self.assertEqual(self.conn.execute(
            'SELECT id, projectId, runId, source, "trigger", action, note, at'
            " FROM interventions WHERE action = 'approve_story'").fetchall(),
            [(intervention, 1, None, "human", "manual", "approve_story",
              "the plan reads right", 50)])

    def test_approval_is_refused_and_writes_nothing(self):
        self.file()
        other = self.ticket("KO-5", revision=1)
        child = self.ticket("KO-6")
        self.conn.commit()
        file_story(self.conn, other, WITNESSES[:1],
                   [(child, "completes", ("W1",))])
        cases = [("revision 2, not 1", self.parent, 1, None)]
        for state in ("approved", "parked"):
            cases.append((f"is {state}; one story is open", self.parent, 2,
                          state))
        for problem, parent, revision, other_state in cases:
            with self.subTest(problem=problem):
                with store.transaction(self.conn):
                    self.conn.execute(
                        "UPDATE stories SET state = COALESCE(?, 'planned')"
                        " WHERE ticketId = ?", (other_state, other))
                before = (self.snapshot(), self.interventions())
                with self.assertRaisesRegex(ValueError, problem):
                    approve_story(self.conn, parent, revision, "operator",
                                  "go")
                self.assertEqual((self.snapshot(), self.interventions()),
                                 before)
        with store.transaction(self.conn):
            self.conn.execute("UPDATE stories SET state = 'planned'"
                              " WHERE ticketId = ?", (other,))
        approve_story(self.conn, self.parent, 2, "operator", "go")
        before = (self.snapshot(), self.interventions())
        with self.assertRaisesRegex(ValueError, "is approved, not planned"):
            approve_story(self.conn, self.parent, 2, "operator", "again")
        self.assertEqual((self.snapshot(), self.interventions()), before)

    def interventions(self):
        return self.conn.execute("SELECT * FROM interventions").fetchall()

    def test_abandon_backlogs_only_the_unclaimed_children(self):
        self.file()
        approve_story(self.conn, self.parent, 2, "operator", "go")
        with store.transaction(self.conn):
            self.conn.execute("UPDATE tickets SET status = 'merged'"
                              " WHERE id = ?", (self.scaffold,))
            run = self.conn.execute(
                "INSERT INTO runs (ticketId, projectId, attempt, phase,"
                " startedAt, lastHeartbeat) VALUES (?, 1, 1, 'working',"
                " 1, 1)", (self.advance,)).lastrowid
            self.conn.execute("UPDATE tickets SET status = 'in_flight',"
                              " activeRunId = ? WHERE id = ?",
                              (run, self.advance))
        untouched = self.conn.execute(
            "SELECT * FROM tickets WHERE id IN (?, ?) ORDER BY id",
            (self.scaffold, self.advance)).fetchall()
        abandon_story(self.conn, self.parent, "the story is given up",
                      "operator", now=70)
        self.assertEqual(story(self.conn, self.parent).state, "abandoned")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM tickets WHERE id = ?",
            (self.parent,)).fetchone(), ("abandoned",))
        self.assertEqual(self.conn.execute(
            "SELECT status, boardColumn, boardState FROM tickets WHERE id = ?",
            (self.complete,)).fetchone(), ("ready", "backlog", "Backlog"))
        self.assertEqual(
            [(note.kind, note.text, note.author)
             for note in ticket_notes(self.conn, self.complete)],
            [("move", "the story is given up", "operator")])
        self.assertEqual(self.conn.execute(
            "SELECT * FROM tickets WHERE id IN (?, ?) ORDER BY id",
            (self.scaffold, self.advance)).fetchall(), untouched)

    def test_abandon_accepts_a_parent_already_walked_abandoned(self):
        self.file()
        store.walk_ticket(self.conn, self.parent, "abandoned")
        abandon_story(self.conn, self.parent, "canceled", "operator")
        self.assertEqual(story(self.conn, self.parent).state, "abandoned")


if __name__ == "__main__":
    unittest.main()
