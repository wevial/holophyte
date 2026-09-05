"""Claim-lease contract for the v2 store (docs/v2/state-model.md §7).

v0 is single-threaded, enforced as a per-project lease: claiming is one
transaction that asserts `projects.activeRunId IS NULL` and records the new
run. These tests read the tables back with their own SQL rather than through
store helpers, so the oracle is the stored state, not the module's own view
of it.

Run: python3 -m unittest discover -s tests -p 'test_store*' -v
"""
from __future__ import annotations

import json
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
            " VALUES ('team_abc', '/repos/holophyte', 'main', 'personal')"
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
            " VALUES ('team_xyz', '/repos/other', 'main', 'personal')"
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


class ContractSnapshotTests(unittest.TestCase):
    """The claim-time freeze of a ticket's contract and the drift check on it.

    The freeze is what makes a merge-time drift check possible at all: without
    it the only readable version of the ticket is the current one, which
    always agrees with itself.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(Path(tmp.name) / "store.sqlite3")
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.ensure_project(self.conn, "team_abc", "/repos/x")

    def mirror(self, title, criteria, commands):
        """Upsert the one issue these tests work with, as the loop mirrors it."""
        return store.mirror_ticket(
            self.conn, self.project_id, "iss_1", "HOL-1", title,
            acceptance_criteria=criteria, verification_commands=commands)

    def test_the_claim_freezes_the_contract_a_later_mirror_cannot_move(self):
        """The run keeps the ticket as it stood when the lease was taken.

        The expected document is transcribed rather than read back through
        `contract_snapshot()`: this is the format the column holds and the
        merge gate compares, so a change to it has to be a deliberate one.
        """
        ticket = self.mirror("ship the thing", ["it ships"], ["make test"])
        run_id = store.claim(self.conn, self.project_id, ticket)

        self.mirror("ship something else", ["it ships", "and logs"], ["make test"])

        self.assertEqual(
            json.loads(store.run_contract(self.conn, run_id)),
            {"title": "ship the thing",
             "acceptanceCriteria": ["it ships"],
             "verificationCommands": ["make test"]})

    def test_drift_names_the_fields_that_moved_and_only_those(self):
        """The gate's actual question, asked across a real edited ticket."""
        ticket = self.mirror("ship the thing", ["it ships"], ["make test"])
        run_id = store.claim(self.conn, self.project_id, ticket)
        claimed = store.run_contract(self.conn, run_id)

        self.assertEqual(
            store.contract_drift(claimed, store.contract_snapshot(
                "ship the thing", ["it ships"], ["make test"])),
            ())
        self.assertEqual(
            store.contract_drift(claimed, store.contract_snapshot(
                "ship something else", ["it ships", "and logs"], ["make test"])),
            ("title", "acceptanceCriteria"))
        # An unreadable side is a comparison that did not happen, not drift:
        # a run claimed before the column existed, or a ticket Linear would
        # not hand back, must not block a merge that verified.
        self.assertEqual(store.contract_drift(None, claimed), ())
        self.assertEqual(store.contract_drift(claimed, None), ())


class ParkTests(ClaimLeaseTests):
    """`park()`: the write behind `[merge] approve = "human"`. The run
    stays alive in the parked phase; the leases come back as `release()`
    gives them back; so the next claim on the project succeeds."""

    def test_park_frees_the_leases_but_does_not_end_the_run(self):
        run_id = store.claim(self.conn, self.project_id, self.ticket_id, now=1700)
        store.set_phase(self.conn, run_id, "merge_gate", now=1800)

        store.park(self.conn, run_id, "awaiting_merge_approval",
                   note="waiting for a human", now=1900)

        self.assertEqual(
            self.conn.execute(
                "SELECT phase, endedAt, outcome, lastHeartbeat FROM runs"
            ).fetchall(),
            [("awaiting_merge_approval", None, None, 1900)])
        self.assertEqual(self.leases(), (None, None))
        self.assertEqual(
            self.conn.execute("SELECT lastRunId FROM tickets").fetchone(),
            (run_id,))
        other = self.add_ticket("iss_2", "HOL-2")
        self.conn.commit()
        self.assertEqual(
            store.claim(self.conn, self.project_id, other, now=2000), run_id + 1)

    def test_park_refuses_an_ended_run_and_a_phase_that_is_not_a_park(self):
        run_id = store.claim(self.conn, self.project_id, self.ticket_id, now=1700)
        with self.assertRaises(ValueError):
            store.park(self.conn, run_id, "merging")
        store.release(self.conn, run_id, "failed", now=1800)
        with self.assertRaises(store.RunEnded):
            store.park(self.conn, run_id, "awaiting_merge_approval")
        self.assertEqual(
            self.conn.execute("SELECT phase FROM runs").fetchone(), ("failed",))


if __name__ == "__main__":
    unittest.main()
