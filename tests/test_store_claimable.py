"""Phase 3 stage 3: `store.read.claimable()`, the store's ready queue in
store mode -- which rows it holds and the two `[loop] order`s.

Run: python3 -m unittest discover -s tests -p 'test_store_claimable.py' -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402 - after the sys.path insert above
import store.read  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above


class ClaimableTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(Path(tmp.name) / "store.sqlite3")
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, "team-1", "/repos/holophyte")

    def ticket(self, identifier, priority=None, column="ready", specced=True,
               depends_on=None):
        return store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"iss-{identifier}",
            linear_identifier=identifier, title=f"ticket {identifier}",
            acceptance_criteria=["Given it, then it works"] if specced else [],
            verification_commands=["echo ok"], priority=priority,
            board_column=column, depends_on=depends_on, labels=["ui"])

    def queue(self, order="identifier"):
        return [row.linearIdentifier
                for row in store.read.claimable(self.conn, self.project_id, order)]

    def test_both_orders(self):
        for identifier, priority in (("KO-10", 0), ("KO-2", 3), ("KO-3", 1),
                                     ("KO-4", None), ("KO-5", 3)):
            self.ticket(identifier, priority)

        self.assertEqual(self.queue(),
                         ["KO-10", "KO-2", "KO-3", "KO-4", "KO-5"])
        self.assertEqual(self.queue("priority"),
                         ["KO-3", "KO-2", "KO-5", "KO-10", "KO-4"])

    def test_only_a_pickable_ready_row_in_the_ready_column_is_queued(self):
        self.ticket("KO-1")
        self.ticket("KO-2", column="backlog")
        self.ticket("KO-3", column=None)
        gone = self.ticket("KO-4")
        store.set_gone_since(self.conn, gone, 1_000)
        leased = self.ticket("KO-5")
        store.claim(self.conn, self.project_id, leased)
        self.ticket("KO-6", specced=False)
        self.ticket("KO-7", depends_on=["iss-KO-1"])

        self.assertEqual(self.queue(), ["KO-1"])
        (row,) = store.read.claimable(self.conn, self.project_id)
        self.assertEqual((row.revision, row.labels, row.acceptanceCriteria,
                          row.verificationCommands),
                         (1, ("ui",), ("Given it, then it works",), ("echo ok",)))


if __name__ == "__main__":
    unittest.main()
