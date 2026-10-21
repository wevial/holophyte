import tempfile
import time
import unittest
from pathlib import Path

import store
import store.board
import store.criteria
import store.steer_notes
import store.tickets
from holophyte.review.criterion_ids import numbered
from tests.test_ticket_criteria_rows import ticket_body

CLAIMED = ["the list shows every entry", "the list is sorted by name"]
REVISED = ["the table shows every row", "the table is sorted by age",
           "the table labels each column"]


class SteerRevisionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = store.open(Path(directory.name) / "store.db")
        self.addCleanup(self.conn.close)
        project = store.tickets.ensure_project(
            self.conn, "native-team", directory.name)
        key = store.board.file_ticket(self.conn, project, "NAT",
                                      ticket_body(CLAIMED))
        self.ticket = self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?", (key,)
        ).fetchone()[0]
        store.tickets.transition(self.conn, self.ticket, "in_flight")
        self.run_id = store.claim(self.conn, project, self.ticket)
        self.edited = int(time.time() * 1000) + 1000
        store.board.edit_ticket(self.conn, project, key, ticket_body(REVISED),
                                expected_revision=1, now=self.edited)

    def test_a_prose_amendment_after_a_revision_adopts_its_criteria(self):
        store.steer_notes.steer(
            self.conn, self.ticket, "Meet revision 2's acceptance criteria",
            "maintainer", now=self.edited + 1000)
        held = store.criteria.numbers(self.conn, self.ticket)
        expected = [(held[text], text) for text in REVISED]
        self.assertEqual(numbered(self.conn, self.run_id, CLAIMED), expected)


if __name__ == "__main__":
    unittest.main()
