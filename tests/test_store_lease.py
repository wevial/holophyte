"""The claim leases the ticket, not the project (KO-341).

Two loops on one target each hold a ticket of their own; a claim naming a
ticket a live run already holds is refused by name. The store is read back
with plain SQL over a second connection, so the oracle is the stored state
and the second claimer is a different process's view of it, as a second loop
would be.

Run: python3 -m unittest discover -s tests -p 'test_store*' -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import store


class TicketLeaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"
        self.conn = self.open()
        store.init(self.conn)
        self.project = store.ensure_project(self.conn, "team_abc", "/repos/x")
        self.tickets = {
            ident: store.mirror_ticket(
                self.conn, self.project, f"iss_{ident}", ident, f"ticket {ident}",
                acceptance_criteria=["it works"], verification_commands=["true"])
            for ident in ("KO-1", "KO-2")}

    def open(self):
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        return conn

    def run_of(self, conn, ident):
        return conn.execute(
            "SELECT activeRunId FROM tickets WHERE linearIdentifier = ?",
            (ident,)).fetchone()[0]

    def test_two_connections_claim_two_tickets_of_one_project(self):
        other = self.open()

        first = store.claim(self.conn, self.project, self.tickets["KO-1"])
        second = store.claim(other, self.project, self.tickets["KO-2"])

        self.assertNotEqual(first, second)
        self.assertEqual(
            self.conn.execute(
                "SELECT id, ticketId, phase FROM runs ORDER BY id").fetchall(),
            [(first, self.tickets["KO-1"], "claimed"),
             (second, self.tickets["KO-2"], "claimed")])
        # Each ticket carries its own run, read over the other connection.
        self.assertEqual(self.run_of(other, "KO-1"), first)
        self.assertEqual(self.run_of(self.conn, "KO-2"), second)

    def test_a_claim_on_a_leased_ticket_is_refused_by_name_and_opens_no_run(self):
        holder = store.claim(self.conn, self.project, self.tickets["KO-1"])
        other = self.open()

        with self.assertRaises(store.ClaimConflict) as refused:
            store.claim(other, self.project, self.tickets["KO-1"])

        self.assertEqual(str(refused.exception),
                         f"ticket KO-1: lease already held by run {holder}")
        self.assertEqual(
            other.execute("SELECT COUNT(*) FROM runs").fetchone(), (1,))
        self.assertEqual(self.run_of(other, "KO-1"), holder)
        # The refusal is the ticket's alone: the project's other ticket is
        # still there for the second connection to take.
        self.assertIsNotNone(
            store.claim(other, self.project, self.tickets["KO-2"]))


if __name__ == "__main__":
    unittest.main()
