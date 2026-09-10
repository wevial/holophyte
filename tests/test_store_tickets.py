"""`tickets.body`: the claim-time mirror keeps the Linear body the loop read,
`ticket_by_identifier()` reads it back, and a schema-10 store gains the
column on open (KO-328).

Run: python3 -m unittest discover -s tests -p 'test_store_tickets*' -v
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402 - after the sys.path insert above
import store.read  # noqa: E402 - after the sys.path insert above


class MirroredBodyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"
        self.conn = store.open(self.path)
        self.addCleanup(self.conn.close)
        self.project = store.ensure_project(self.conn, "team-1",
                                            "/repos/holophyte")

    def mirror(self, body, **overrides):
        args = dict(linear_issue_id="issue-1", linear_identifier="KO-1",
                    title="ticket 1",
                    acceptance_criteria=["Given KO-1, then it is worked"],
                    verification_commands=["echo ok"], time_box_ms=1_500_000)
        args.update(overrides)
        return store.mirror_ticket(self.conn, self.project, body=body, **args)

    def test_a_re_mirror_replaces_the_body_and_leaves_the_status_alone(self):
        ticket_id = self.mirror("first", now=1_700_000_000_000)
        store.transition(self.conn, ticket_id, "in_flight")

        self.assertEqual(self.mirror("second", now=1_700_000_060_000),
                         ticket_id)

        ticket = store.read.ticket_by_identifier(self.conn, "KO-1")
        self.assertEqual(ticket.body, "second")
        self.assertEqual(ticket.status, "in_flight")
        self.assertEqual(ticket.linearIdentifier, "KO-1")
        self.assertEqual(ticket.title, "ticket 1")
        self.assertEqual(ticket.acceptanceCriteria,
                         ("Given KO-1, then it is worked",))
        self.assertEqual(ticket.verificationCommands, ("echo ok",))
        self.assertEqual(ticket.timeBoxMs, 1_500_000)
        self.assertIsNone(ticket.activeRunId)
        self.assertEqual(ticket.mirroredAt, 1_700_000_060_000)

    def test_an_identifier_never_mirrored_is_none(self):
        self.mirror("first")
        self.assertIsNone(store.read.ticket_by_identifier(self.conn, "KO-9999"))

    def test_a_claimed_ticket_names_its_active_run(self):
        ticket_id = self.mirror("body")
        store.transition(self.conn, ticket_id, "in_flight")
        run_id = store.claim(self.conn, self.project, ticket_id,
                             now=1_700_000_000_000)
        self.assertEqual(
            store.read.ticket_by_identifier(self.conn, "KO-1").activeRunId,
            run_id)


class Version10BodyMigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"

    def user_version(self):
        raw = sqlite3.connect(self.path)
        try:
            return raw.execute("PRAGMA user_version").fetchone()[0]
        finally:
            raw.close()

    def test_a_version_10_store_gains_an_empty_body_and_keeps_its_ticket(self):
        """A store stamped 10 has no `tickets.body`; opening it with this
        build adds the column, stamps version 11, and the ticket it held is
        still there with an empty body."""
        conn = store.open(self.path)
        project = store.ensure_project(conn, "team-1", "/repos/holophyte")
        store.mirror_ticket(
            conn, project, linear_issue_id="issue-1", linear_identifier="KO-1",
            title="ticket 1", acceptance_criteria=["Given KO-1, then worked"],
            verification_commands=["echo ok"], body="the body the loop read")
        conn.execute("ALTER TABLE tickets DROP COLUMN body")
        conn.execute("PRAGMA user_version = 10")
        conn.commit()
        conn.close()
        raw = sqlite3.connect(self.path)
        columns = {row[1] for row in raw.execute("PRAGMA table_info(tickets)")}
        raw.close()
        self.assertNotIn("body", columns)
        self.assertEqual(self.user_version(), 10)

        conn = store.open(self.path)
        self.addCleanup(conn.close)

        self.assertEqual(store.SCHEMA_VERSION, 11)
        self.assertEqual(self.user_version(), 11)
        ticket = store.read.ticket_by_identifier(conn, "KO-1")
        self.assertEqual(ticket.title, "ticket 1")
        self.assertEqual(ticket.status, "ready")
        self.assertEqual(ticket.body, "")


if __name__ == "__main__":
    unittest.main()
