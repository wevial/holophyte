"""A story's parent keeps its contract withheld on every write, so it is
never claimable, while a plain ticket is routed by its contract as before.

Run: python3 -m unittest discover -s tests -p 'test_story_parent.py' -v
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402 - after the sys.path insert above
import store.board  # noqa: E402 - after the sys.path insert above
import store.read  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from tests.test_store_board import body  # noqa: E402 - after the insert


class StoryParentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.conn = store.open(Path(tmp.name) / "store.sqlite3")
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, "team-1", str(repo))

    def mirror(self, n, contract=True, text=None):
        return store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{n}",
            linear_identifier=f"KO-{n}", title=f"ticket {n}",
            acceptance_criteria=["Given it, then it holds"] if contract else (),
            verification_commands=["echo ok"] if contract else (),
            body=text or f"body {n}", board_column="ready")

    def make_parent(self, ticket_id):
        self.conn.execute("INSERT INTO stories (ticketId, state)"
                          " VALUES (?, 'planned')", (ticket_id,))
        self.conn.commit()

    def row(self, ticket_id):
        status, criteria, commands, text, revision = self.conn.execute(
            "SELECT status, acceptanceCriteria, verificationCommands, body,"
            " revision FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return status, json.loads(criteria), json.loads(commands), text, revision

    def claimable(self):
        return {ticket.linearIdentifier for ticket in
                store.read.claimable(self.conn, self.project_id)}

    def test_a_re_mirror_with_a_contract_leaves_a_parent_unclaimable(self):
        parent = self.mirror(1, contract=False)
        plain = self.mirror(2, contract=False)
        merged = self.mirror(3)
        self.make_parent(parent)
        self.make_parent(merged)
        store.tickets.walk_ticket(self.conn, merged, "merged")

        self.mirror(1, text="the parent, edited")
        self.mirror(2)
        self.mirror(3)

        self.assertEqual(self.row(parent)[:4],
                         ("needs_spec", [], [], "the parent, edited"))
        self.assertEqual(self.row(plain)[0], "ready")
        self.assertEqual(self.row(merged)[0], "merged")
        self.assertEqual(self.claimable(), {"KO-2"})

    def test_a_native_parent_edited_into_a_valid_ticket_stays_needs_spec(self):
        identifier = store.board.file_ticket(
            self.conn, self.project_id, "NAT", body("The parent"))
        (parent,) = self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?",
            (identifier,)).fetchone()
        self.make_parent(parent)
        before = self.row(parent)[4]

        revision = store.board.edit_ticket(
            self.conn, self.project_id, identifier, body("The parent, edited"),
            before)

        status, criteria, commands, text, _ = self.row(parent)
        self.assertEqual((status, criteria, commands),
                         ("needs_spec", [], []))
        self.assertIn("The parent, edited", text)
        self.assertEqual(revision, before + 1)
        self.assertNotIn(identifier, self.claimable())


if __name__ == "__main__":
    unittest.main()
