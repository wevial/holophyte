"""KO-736: the mirror path records a ticket's board-owned fields and writes
a `ticketRevisions` row whenever one of them changes.

Run: python3 -m unittest discover -s tests -p 'test_store_revisions.py' -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import holophyte.board  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above


def listing_task(**changes):
    """One task in the shape `linear_provider.ready_issues()` hands over."""
    task = {"id": "KO-1", "issue_id": "iss-1", "title": "add a thing",
            "verify": "echo ok", "budget_min": 5, "contracts": [],
            "criteria": ["Given the thing, when it runs, then it works"],
            "body": "## Summary\n\nThe thing.\n", "priority": 3,
            "labels": ["ui", "backend"], "filed_at": 1_000, "updatedAt": 2_000,
            "board_state": "Todo"}
    task.update(changes)
    return task


class MirrorRevisionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(Path(tmp.name) / "store.sqlite3")
        self.addCleanup(self.conn.close)
        self.project = store.tickets.ensure_project(self.conn, "team-1",
                                                    "/repos/holophyte")

    def mirror(self, task):
        return holophyte.board.mirror_task(self.conn, self.project, task)

    def revisions(self, ticket):
        return [(n, author, title, body, priority, json.loads(labels), column)
                for n, author, title, body, priority, labels, column
                in self.conn.execute(
                    "SELECT revision, author, title, body, priority, labels,"
                    " boardColumn FROM ticketRevisions WHERE ticketId = ?"
                    " ORDER BY revision", (ticket,))]

    def current(self, ticket):
        return self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?", (ticket,)).fetchone()[0]

    def test_each_board_owned_change_is_the_next_revision(self):
        body = "## Summary\n\nThe thing.\n"
        ticket = self.mirror(listing_task())
        self.mirror(listing_task())
        self.assertEqual(self.revisions(ticket), [
            (1, "board", "add a thing", body, 3, ["ui", "backend"], "ready")])

        self.mirror(listing_task(title="add the thing"))
        self.assertEqual(self.revisions(ticket)[1:], [
            (2, "board", "add the thing", body, 3, ["ui", "backend"], "ready")])
        self.assertEqual(self.current(ticket), 2)

        # The factory's own labels are not the board's: no revision.
        self.mirror(listing_task(title="add the thing", labels=[
            "holo:writer", "ui", "stale", "backend"]))
        self.assertEqual(len(self.revisions(ticket)), 2)

        self.mirror(listing_task(title="add the thing", priority=1))
        self.assertEqual(self.revisions(ticket)[2:], [
            (3, "board", "add the thing", body, 1, ["ui", "backend"], "ready")])
        self.assertEqual(self.current(ticket), 3)

    def test_an_older_builds_write_is_healed_as_one_unrecorded_revision(self):
        ticket = self.mirror(listing_task())
        # The previous build's mirror_ticket() UPDATE, frozen.
        self.conn.execute(
            "UPDATE tickets SET linearIdentifier = ?, title = ?, body = ?,"
            " status = ?, acceptanceCriteria = ?, verificationCommands = ?,"
            " timeBoxMs = ?, affinity = ?,"
            " dependsOn = COALESCE(?, dependsOn), mirroredAt = ?,"
            " url = ?, boardState = ?"
            " WHERE id = ?",
            ("KO-1", "retitled", "new body", "ready", '["a"]', '["echo ok"]',
             300_000, "any", None, 5, None, "Todo", ticket))
        # The previous build's mirror_ticket() INSERT, frozen: revision 0.
        inserted = self.conn.execute(
            "INSERT INTO tickets"
            " (projectId, linearIssueId, linearIdentifier, title, body,"
            "  status, acceptanceCriteria, verificationCommands, timeBoxMs,"
            "  affinity, dependsOn, mirroredAt, url, boardState)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.project, "iss-2", "KO-2", "second", "", "ready", '["a"]',
             '["echo ok"]', None, "any", "[]", 3, None, "Todo")).lastrowid
        self.conn.commit()

        self.mirror(listing_task(title="retitled", body="new body"))
        store.tickets.mirror_ticket(
            self.conn, self.project, "iss-2", "KO-2", "second",
            acceptance_criteria=["a"], verification_commands=["echo ok"])

        self.assertEqual(self.revisions(ticket)[1:], [
            (2, "unrecorded", "retitled", "new body", 3, ["ui", "backend"],
             "ready")])
        self.assertEqual(self.current(ticket), 2)
        self.assertEqual(self.revisions(inserted), [
            (1, "unrecorded", "second", "", None, [], None)])
        self.assertEqual(self.current(inserted), 1)

    def test_a_task_without_priority_or_labels_keeps_the_stored_ones(self):
        ticket = self.mirror(listing_task(priority=2, labels=["ui"]))
        # As the file board hands a task over: neither key.
        task = listing_task()
        del task["priority"], task["labels"]
        self.mirror(task)
        self.assertEqual(self.conn.execute(
            "SELECT priority, labels FROM tickets WHERE id = ?",
            (ticket,)).fetchone(), (2, '["ui"]'))
        self.assertEqual(len(self.revisions(ticket)), 1)


if __name__ == "__main__":
    unittest.main()
