"""A story's generation: raised by a child's merge, stamped on a child's run."""
import json
import tempfile
import unittest
from pathlib import Path

import store
from store.stories import advance_story, approve_story, file_story
from store.tickets import transition, walk_ticket

WITNESSES = [
    {"key": "W1", "criterion": "orders export", "file": "tests/test_w1.py",
     "command": "python3 -m unittest tests.test_w1", "source": "assert 1\n"},
    {"key": "W2", "criterion": "one file", "file": "tests/test_w2.py",
     "command": "python3 -m unittest tests.test_w2", "source": "assert 2\n"},
]


class StoryGenerationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(Path(tmp.name) / "store.db")
        self.addCleanup(self.conn.close)
        self.project = self.conn.execute(
            "INSERT INTO projects (linearTeamId, repoPath, defaultBranch,"
            " autonomyProfile) VALUES ('t', '/repo', 'main', 'personal')"
        ).lastrowid
        self.parent = self.ticket("KO-1")
        self.first = self.ticket("KO-2")
        self.second = self.ticket("KO-3")
        self.lone = self.ticket("KO-9")
        self.conn.commit()
        file_story(self.conn, self.parent, WITNESSES,
                   [(self.first, "advances", ("W1", "W2")),
                    (self.second, "completes", ("W1", "W2"))])
        approve_story(self.conn, self.parent, 1, "operator", "go")

    def ticket(self, identifier):
        return self.conn.execute(
            "INSERT INTO tickets (projectId, linearIssueId, linearIdentifier,"
            " title, mirroredAt, status, affinity, revision, dependsOn)"
            " VALUES (?, ?, ?, 't', 1, 'ready', 'any', 1, ?)",
            (self.project, identifier, identifier, json.dumps([]))).lastrowid

    def generation(self):
        return self.conn.execute(
            "SELECT generation FROM stories WHERE ticketId = ?",
            (self.parent,)).fetchone()[0]

    def status(self, ticket_id):
        return self.conn.execute("SELECT status FROM tickets WHERE id = ?",
                                 (ticket_id,)).fetchone()[0]

    def test_each_child_merge_raises_the_generation_by_one(self):
        self.assertEqual(self.generation(), 0)
        transition(self.conn, self.first, "in_flight")
        transition(self.conn, self.first, "merged")
        self.assertEqual(self.generation(), 1)
        walk_ticket(self.conn, self.second, "merged")
        self.assertEqual(self.generation(), 2)

    def test_a_child_with_two_roles_counts_once_and_a_lone_ticket_none(self):
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM storyChildren WHERE ticketId = ?",
            (self.first,)).fetchone()[0], 2)
        walk_ticket(self.conn, self.first, "merged")
        self.assertEqual(self.generation(), 1)
        before = self.conn.execute("SELECT * FROM stories").fetchall()
        walk_ticket(self.conn, self.lone, "merged")
        self.assertEqual(self.status(self.lone), "merged")
        self.assertEqual(
            self.conn.execute("SELECT * FROM stories").fetchall(), before)

    def test_the_bump_commits_and_rolls_back_with_the_callers_block(self):
        with store.transaction(self.conn):
            walk_ticket(self.conn, self.first, "merged")
        self.assertEqual(self.generation(), 1)
        with self.assertRaises(RuntimeError):
            with store.transaction(self.conn):
                walk_ticket(self.conn, self.second, "merged")
                raise RuntimeError("after the walk")
        self.assertEqual(self.status(self.second), "ready")
        self.assertEqual(self.generation(), 1)

    def test_a_merge_advanced_before_its_close_out_is_counted_once(self):
        child_run = store.claim(self.conn, self.project, self.first)
        lone_run = store.claim(self.conn, self.project, self.lone)
        self.assertEqual(advance_story(self.conn, child_run), 1)
        self.assertIsNone(advance_story(self.conn, lone_run))
        walk_ticket(self.conn, self.first, "merged")
        self.assertEqual(self.generation(), 1)
        walk_ticket(self.conn, self.second, "merged")
        self.assertEqual(self.generation(), 2)

    def test_a_claim_stamps_its_storys_generation_on_the_run(self):
        self.conn.execute("UPDATE stories SET generation = 3")
        self.conn.commit()
        child_run = store.claim(self.conn, self.project, self.first)
        lone_run = store.claim(self.conn, self.project, self.lone)
        stamped = dict(self.conn.execute(
            "SELECT id, storyGeneration FROM runs").fetchall())
        self.assertEqual(stamped, {child_run: 3, lone_run: None})


if __name__ == "__main__":
    unittest.main()
