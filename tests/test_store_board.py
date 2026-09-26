"""KO-750: `store.board` files and edits a native ticket. Filing numbers it
`KEY-n` from `projects.ticketSeq`, validates it as `--file-ticket` does and
resolves `Depends on:` to the project's own tickets; an edit is refused at
a stale revision. Real SQLite files, a real git repository as the project's.

Run: python3 -m unittest discover -s tests -p 'test_store_board.py' -v
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402 - after the sys.path insert above
import store.board  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above


def body(title="Add export endpoint", depends="none", what=True):
    what_line = ("**What:** GET /orders.csv streams the current user's orders"
                 " as CSV.\n\n") if what else ""
    return f"""\
# {title}

## Summary

Add a CSV export endpoint for the orders list.

## What / Why / How

{what_line}**Why:** Ops needs orders in spreadsheets without database access.

**How:** Reuse the orders query service and stream via the csv module.

## In scope

- CSV serialization of the orders list

## Out of scope

- Excel-specific formatting

## Acceptance criteria

- [ ] Given 3 orders, when GET /orders.csv, then 4 lines including header.

## Verify command(s)

```
python3 -m unittest test_orders_export
```

## Implementation notes

- Endpoint lives beside the other order routes.

## Estimate & dependencies

Estimate: 25 min · Depends on: {depends}

## Open questions

- None
"""


class StoreBoardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.path = Path(tmp.name) / "store.sqlite3"
        self.conn = self.open()
        self.project_id = store.tickets.ensure_project(
            self.conn, "team-1", str(repo))

    def open(self):
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        return conn

    def seq(self):
        return self.conn.execute("SELECT ticketSeq FROM projects WHERE id = ?",
                                 (self.project_id,)).fetchone()[0]

    def row(self, identifier):
        return self.conn.execute(
            "SELECT linearIssueId, status, boardColumn, revision, dependsOn,"
            " title, priority FROM tickets WHERE linearIdentifier = ?",
            (identifier,)).fetchone()

    def revisions(self, identifier):
        return self.conn.execute(
            "SELECT r.revision, r.author, r.title, r.priority"
            " FROM ticketRevisions r JOIN tickets t ON t.id = r.ticketId"
            " WHERE t.linearIdentifier = ? ORDER BY r.revision",
            (identifier,)).fetchall()

    def ticket_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]

    def test_two_filers_at_once_get_two_numbers_and_a_bad_body_uses_none(self):
        barrier = threading.Barrier(2)
        answers = []

        def filer(title):
            conn = store.open(self.path)
            try:
                barrier.wait()
                answers.append(store.board.file_ticket(
                    conn, self.project_id, "NAT", body(title)))
            except Exception as e:  # noqa: BLE001 - the test reads it
                answers.append(e)
            finally:
                conn.close()

        threads = [threading.Thread(target=filer, args=(f"Filer {n}",))
                   for n in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(answers), ["NAT-1", "NAT-2"])
        self.assertEqual(self.seq(), 2)
        for identifier in ("NAT-1", "NAT-2"):
            board_id, status, column, revision, _, _, _ = self.row(identifier)
            self.assertEqual((board_id, status, column, revision),
                             (identifier, "ready", "ready", 1))
            self.assertEqual([r[:2] for r in self.revisions(identifier)],
                             [(1, "cli")])

        with self.assertRaises(store.board.FilingRefused) as refused:
            store.board.file_ticket(self.conn, self.project_id, "NAT",
                                    body(what=False))
        self.assertIn("What", str(refused.exception))
        self.assertEqual(str(refused.exception), refused.exception.problems[0])
        self.assertEqual(self.ticket_count(), 2)
        self.assertEqual(self.seq(), 2)

    def test_dependencies_resolve_to_the_projects_own_tickets(self):
        store.board.file_ticket(self.conn, self.project_id, "NAT", body())

        waiting = store.board.file_ticket(self.conn, self.project_id, "NAT",
                                          body(depends="NAT-1"))
        _, status, _, _, depends, _, _ = self.row(waiting)
        self.assertEqual((status, json.loads(depends)),
                         ("blocked_on_deps", ["NAT-1"]))

        with self.assertRaises(store.board.FilingRefused) as refused:
            store.board.file_ticket(self.conn, self.project_id, "NAT",
                                    body(depends="NAT-9"))
        self.assertIn("NAT-9", str(refused.exception))
        self.assertEqual(self.ticket_count(), 2)
        self.assertEqual(self.seq(), 2)

        draft = store.board.file_ticket(self.conn, self.project_id, "NAT",
                                        body(what=False), column="backlog")
        _, status, column, _, _, _, _ = self.row(draft)
        self.assertEqual((status, column), ("needs_spec", "backlog"))

    def test_an_edit_at_a_stale_revision_writes_nothing(self):
        store.board.file_ticket(self.conn, self.project_id, "NAT", body())

        revision = store.board.edit_ticket(
            self.conn, self.project_id, "NAT-1", body("Export orders"),
            expected_revision=1, author="console", priority=2)
        self.assertEqual(revision, 2)
        self.assertEqual(self.row("NAT-1")[3:],
                         (2, "[]", "Export orders", 2))
        self.assertEqual(self.revisions("NAT-1")[-1],
                         (2, "console", "Export orders", 2))

        row, revisions = self.row("NAT-1"), self.revisions("NAT-1")
        with self.assertRaises(store.RevisionMoved) as moved:
            store.board.edit_ticket(
                self.conn, self.project_id, "NAT-1", body("Export again"),
                expected_revision=1, priority=3)
        self.assertEqual(moved.exception.current, 2)
        self.assertEqual(self.row("NAT-1"), row)
        self.assertEqual(self.revisions("NAT-1"), revisions)


if __name__ == "__main__":
    unittest.main()
