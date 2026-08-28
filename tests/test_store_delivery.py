"""Webhook delivery idempotency for the v2 store (docs/v2/state-model.md §1).

Every inbound Linear delivery id is recorded in `linearDeliveries` in the same
transaction as its effect, so a replayed delivery is dropped instead of
double-processed. The three interesting states are: first delivery (effect and
id commit together), replay (effect skipped, nothing changes), and a *failed*
effect (id released with the rollback, so a retry still processes).

Committed state is read back through a second connection on purpose. Asserting
on the writing connection would show its own uncommitted transaction, which is
exactly the thing these tests must distinguish from a commit.

Run: python3 -m unittest discover -s tests -p 'test_store*' -v
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import store


class WithDeliveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"
        self.conn = self.open()
        store.init(self.conn)
        self.project_id = self.conn.execute(
            "INSERT INTO projects"
            " (linearTeamId, repoPath, defaultBranch, autonomyProfile)"
            " VALUES ('team_abc', '/srv/dev/holophyte', 'main', 'personal')"
        ).lastrowid
        self.conn.commit()

    def open(self):
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        return conn

    def mirror(self, linear_issue_id):
        """A trivial effect: mirror one Linear issue into `tickets`.

        Stands in for whatever a webhook actually does. `.calls` records each
        invocation, so a skipped effect is observable as an empty list rather
        than only as an absent row.
        """

        def effect(conn):
            effect.calls.append(linear_issue_id)
            conn.execute(
                "INSERT INTO tickets"
                " (projectId, linearIssueId, linearIdentifier, title, status,"
                "  affinity, mirroredAt)"
                " VALUES (?, ?, 'HOL-1', 'a ticket', 'ready', 'any', 0)",
                (self.project_id, linear_issue_id),
            )
            return linear_issue_id

        effect.calls = []
        return effect

    def committed(self):
        """(mirrored issue ids, delivery rows) as another connection sees them."""
        other = self.open()
        return (
            [row[0] for row in other.execute(
                "SELECT linearIssueId FROM tickets ORDER BY id"
            )],
            other.execute(
                "SELECT deliveryId, processedAt FROM linearDeliveries"
                " ORDER BY deliveryId"
            ).fetchall(),
        )

    def test_a_fresh_delivery_commits_its_effect_and_its_id_together(self):
        effect = self.mirror("iss_1")

        outcome = store.with_delivery(self.conn, "del_1", effect, now=1700)

        self.assertFalse(outcome.replayed)
        self.assertEqual(outcome.result, "iss_1")
        self.assertEqual(effect.calls, ["iss_1"])
        self.assertEqual(self.committed(), (["iss_1"], [("del_1", 1700)]))

    def test_a_replayed_delivery_skips_the_effect_and_changes_nothing(self):
        store.with_delivery(self.conn, "del_1", self.mirror("iss_1"), now=1700)
        before = self.committed()
        effect = self.mirror("iss_2")

        outcome = store.with_delivery(self.conn, "del_1", effect, now=1800)

        self.assertTrue(outcome.replayed)
        self.assertIsNone(outcome.result)
        self.assertEqual(effect.calls, [])
        # Including processedAt: the replay does not restamp the original row.
        self.assertEqual(self.committed(), before)

    def test_a_failed_effect_releases_the_id_so_a_retry_processes(self):
        def explodes(conn):
            conn.execute(
                "INSERT INTO tickets"
                " (projectId, linearIssueId, linearIdentifier, title, status,"
                "  affinity, mirroredAt)"
                " VALUES (?, 'iss_1', 'HOL-1', 'a ticket', 'ready', 'any', 0)",
                (self.project_id,),
            )
            raise RuntimeError("effect failed halfway")

        with self.assertRaises(RuntimeError):
            store.with_delivery(self.conn, "del_1", explodes, now=1700)

        # The half-written ticket rolled back with the unrecorded delivery id.
        self.assertEqual(self.committed(), ([], []))

        retry = store.with_delivery(self.conn, "del_1", self.mirror("iss_1"), now=1900)

        self.assertFalse(retry.replayed)
        self.assertEqual(self.committed(), (["iss_1"], [("del_1", 1900)]))

    def test_an_integrity_error_from_the_effect_is_not_read_as_a_replay(self):
        # `linearIssueId` is UNIQUE, so this second delivery's effect raises the
        # same exception type the duplicate-id guard catches. Swallowing it would
        # report a brand-new delivery as already processed and drop it forever.
        store.with_delivery(self.conn, "del_1", self.mirror("iss_1"), now=1700)
        effect = self.mirror("iss_1")

        with self.assertRaises(sqlite3.IntegrityError):
            store.with_delivery(self.conn, "del_2", effect, now=1800)

        self.assertEqual(effect.calls, ["iss_1"])
        self.assertEqual(self.committed(), (["iss_1"], [("del_1", 1700)]))

    def test_a_failed_commit_rolls_back_so_the_connection_stays_usable(self):
        # `tickets.activeRunId` is DEFERRABLE INITIALLY DEFERRED, so a dangling
        # reference survives the INSERT and only fails at COMMIT. SQLite leaves
        # the transaction open on that failure: unrolled back, the delivery id
        # is reserved but uncommitted and the next BEGIN IMMEDIATE raises
        # "cannot start a transaction within a transaction".
        def dangling(conn):
            conn.execute(
                "INSERT INTO tickets"
                " (projectId, linearIssueId, linearIdentifier, title, status,"
                "  affinity, mirroredAt, activeRunId)"
                " VALUES (?, 'iss_1', 'HOL-1', 'a ticket', 'ready', 'any', 0,"
                "         999999)",
                (self.project_id,),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            store.with_delivery(self.conn, "del_1", dangling, now=1700)

        self.assertEqual(self.committed(), ([], []))

        retry = store.with_delivery(self.conn, "del_1", self.mirror("iss_1"), now=1900)

        self.assertFalse(retry.replayed)
        self.assertEqual(self.committed(), (["iss_1"], [("del_1", 1900)]))

    def test_an_unusable_delivery_id_is_refused_before_anything_runs(self):
        # SQLite permits NULL in a TEXT PRIMARY KEY, and permits it repeatedly,
        # so a missing id would silently defeat the dedup instead of colliding.
        for bad in (None, "", b"del_1", 7):
            with self.subTest(delivery_id=bad):
                effect = self.mirror("iss_1")

                with self.assertRaises(ValueError):
                    store.with_delivery(self.conn, bad, effect)

                self.assertEqual(effect.calls, [])
        self.assertEqual(self.committed(), ([], []))


if __name__ == "__main__":
    unittest.main()
