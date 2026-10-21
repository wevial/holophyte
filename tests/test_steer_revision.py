import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import store
import store.board
import store.criteria
import store.steer_notes
import store.tickets
from holophyte.loop import adjudicate, runs
from holophyte.loop.gates import RunFailure
from holophyte.review.criterion_ids import numbered
from tests.test_ticket_criteria_rows import ticket_body

CLAIMED = ["the list shows every entry", "the list is sorted by name"]
REVISED = ["the table shows every row", "the table is sorted by age",
           "the table labels each column"]


class SteerRevisionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
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

    def held(self, texts):
        held = store.criteria.numbers(self.conn, self.ticket)
        return [(held[text], text) for text in texts]

    def amend(self):
        store.steer_notes.steer(
            self.conn, self.ticket, "Meet revision 2's acceptance criteria",
            "maintainer", now=self.edited + 1000)

    def test_a_prose_amendment_after_a_revision_adopts_its_criteria(self):
        self.amend()
        self.assertEqual(numbered(self.conn, self.run_id, CLAIMED),
                         self.held(REVISED))

    def test_terminal_adjudication_judges_the_adopted_criteria(self):
        self.amend()
        revised = self.held(REVISED)
        listed = "\n".join(f"C{n}. {text}" for n, text in revised)
        amended = "\n".join(f"- **C{n}** {text}" for n, text in revised)
        second = revised[1][0]
        store.set_phase(self.conn, self.run_id, "working")
        reply = (f"CRITERION C{second}: not met \u2014 no age sort\n"
                 "VERDICT: FAIL")
        with patch.object(adjudicate, "agent", return_value=reply) as agent, \
                patch.object(runs, "agent_route", return_value="adjudicator"):
            with self.assertRaises(RunFailure) as failed:
                adjudicate._terminal_adjudication(
                    None, self.conn, self.run_id, None, "NAT-1", {}, "main",
                    self.root, 60, "base", "head", ticket_body(CLAIMED),
                    "echo ok", [], 1, criteria=CLAIMED,
                    resume={"terminal": True, "ok": True, "out": "ok"})
        prompt = agent.call_args.args[2]
        self.assertIn(f"Acceptance criteria, numbered:\n{listed}\n\n", prompt)
        self.assertIn(f"Amended acceptance criteria:\n{amended}", prompt)
        reason = str(failed.exception)
        self.assertIn(f"C{second}", reason)
        self.assertIn(REVISED[1], reason)
        for text in CLAIMED:
            self.assertNotIn(text, reason)

    def test_without_an_amendment_the_claimed_criteria_stand(self):
        claimed = self.held(CLAIMED)
        self.assertEqual(numbered(self.conn, self.run_id, CLAIMED), claimed)
        store.steer_notes.steer(
            self.conn, self.ticket, "Sort by age first", "maintainer",
            hint=True, now=self.edited + 1000)
        self.assertEqual(numbered(self.conn, self.run_id, CLAIMED), claimed)


if __name__ == "__main__":
    unittest.main()
