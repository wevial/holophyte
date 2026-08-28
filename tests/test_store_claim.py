"""Claim-lease contract for the v2 store (docs/v2/state-model.md §7).

v0 is single-threaded, enforced as a per-project lease: claiming is one
transaction that asserts `projects.activeRunId IS NULL` and records the new
run. These tests read the tables back with their own SQL rather than through
store helpers, so the oracle is the stored state, not the module's own view
of it.

Run: python3 -m unittest discover -s tests -p 'test_store*' -v
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

import store


class ClaimLeaseTests(unittest.TestCase):
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
        self.ticket_id = self.add_ticket("iss_1", "HOL-1")
        self.conn.commit()

    def open(self):
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        return conn

    def add_ticket(self, linear_issue_id, identifier):
        return self.conn.execute(
            "INSERT INTO tickets"
            " (projectId, linearIssueId, linearIdentifier, title, status,"
            "  affinity, mirroredAt)"
            " VALUES (?, ?, ?, 'a ticket', 'ready', 'any', 0)",
            (self.project_id, linear_issue_id, identifier),
        ).lastrowid

    def runs(self):
        return self.conn.execute(
            "SELECT id, ticketId, projectId, attempt, phase, startedAt,"
            " lastHeartbeat FROM runs ORDER BY id"
        ).fetchall()

    def leases(self):
        return (
            self.conn.execute(
                "SELECT activeRunId FROM projects WHERE id = ?",
                (self.project_id,),
            ).fetchone()[0],
            self.conn.execute(
                "SELECT activeRunId FROM tickets WHERE id = ?", (self.ticket_id,)
            ).fetchone()[0],
        )

    def test_claim_records_a_claimed_run_and_takes_both_leases(self):
        run_id = store.claim(self.conn, self.project_id, self.ticket_id, now=1700)

        self.assertEqual(
            self.runs(),
            [(run_id, self.ticket_id, self.project_id, 1, "claimed", 1700, 1700)],
        )
        self.assertEqual(self.leases(), (run_id, run_id))

    def test_a_second_claim_loses_and_mutates_nothing(self):
        run_id = store.claim(self.conn, self.project_id, self.ticket_id)
        other_ticket = self.add_ticket("iss_2", "HOL-2")
        self.conn.commit()
        before = self.runs()

        with self.assertRaises(store.ClaimConflict):
            store.claim(self.conn, self.project_id, other_ticket)

        # No orphan run row, and the incumbent still holds the lease.
        self.assertEqual(self.runs(), before)
        self.assertEqual(self.leases(), (run_id, run_id))
        self.assertIsNone(
            self.conn.execute(
                "SELECT activeRunId FROM tickets WHERE id = ?", (other_ticket,)
            ).fetchone()[0]
        )

    def test_concurrent_claims_produce_exactly_one_winner(self):
        # Each thread needs its own connection; sqlite3 connections are not
        # shared across threads. The barrier makes them collide on purpose.
        tickets = [self.ticket_id, self.add_ticket("iss_2", "HOL-2")]
        self.conn.commit()
        start = threading.Barrier(len(tickets))
        outcomes = {}

        def claim(ticket_id):
            conn = store.open(self.path)
            try:
                start.wait()
                outcomes[ticket_id] = store.claim(conn, self.project_id, ticket_id)
            except Exception as exc:  # noqa: BLE001 - the loser's error is the assertion
                outcomes[ticket_id] = exc
            finally:
                conn.close()

        threads = [threading.Thread(target=claim, args=(t,)) for t in tickets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(outcomes), len(tickets), "a claim thread never finished")
        winners = [t for t, out in outcomes.items() if not isinstance(out, Exception)]
        losers = [out for out in outcomes.values() if isinstance(out, Exception)]
        self.assertEqual(len(winners), 1, outcomes)
        # The loser lost on the lease, not on a lock timeout: serializing the
        # claim is the point, and "database is locked" would not be that.
        self.assertIsInstance(losers[0], store.ClaimConflict)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.leases()[0], outcomes[winners[0]])

    def test_attempt_counts_the_tickets_prior_runs(self):
        first = store.claim(self.conn, self.project_id, self.ticket_id)
        # Stand in for the run ending: the lease is released, the ticket is
        # free again. Releasing is a later ticket's API.
        self.conn.execute(
            "UPDATE projects SET activeRunId = NULL WHERE id = ?", (self.project_id,)
        )
        self.conn.execute(
            "UPDATE tickets SET activeRunId = NULL WHERE id = ?", (self.ticket_id,)
        )
        self.conn.commit()

        second = store.claim(self.conn, self.project_id, self.ticket_id)

        self.assertEqual(
            self.conn.execute(
                "SELECT id, attempt FROM runs ORDER BY attempt"
            ).fetchall(),
            [(first, 1), (second, 2)],
        )

    def test_claiming_another_projects_ticket_is_refused(self):
        other_project = self.conn.execute(
            "INSERT INTO projects"
            " (linearTeamId, repoPath, defaultBranch, autonomyProfile)"
            " VALUES ('team_xyz', '/srv/dev/other', 'main', 'personal')"
        ).lastrowid
        self.conn.commit()

        with self.assertRaises(store.ClaimConflict):
            store.claim(self.conn, other_project, self.ticket_id)

        self.assertEqual(self.runs(), [])
        self.assertEqual(self.leases(), (None, None))
        self.assertIsNone(
            self.conn.execute(
                "SELECT activeRunId FROM projects WHERE id = ?", (other_project,)
            ).fetchone()[0]
        )


if __name__ == "__main__":
    unittest.main()
