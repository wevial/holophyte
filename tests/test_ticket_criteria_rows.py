import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import store
import store.board
import store.schema
import store.tickets
from tests.test_store_board import body

CRITERIA = [
    "The saved values are covered in `tests/test_store_board.py`.",
    "The formatter passes `ruff check .`.",
    "The data reference explains the new column.",
]
COMMANDS = "python3 -m unittest test_orders_export\nruff check ."


def ticket_body(criteria):
    before, _, tail = body().partition("## Acceptance criteria\n")
    _, _, after = tail.partition("## Verify command(s)")
    return (before + "## Acceptance criteria\n\n"
            + "\n".join(f"- [ ] {text}" for text in criteria)
            + "\n\n## Verify command(s)" + after).replace(
                "python3 -m unittest test_orders_export", COMMANDS)


class TicketCriteriaRowsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "store.db"
        self.conn = store.open(self.path)
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, "native-team", directory.name)

    def file(self, criteria=CRITERIA):
        identifier = store.board.file_ticket(
            self.conn, self.project_id, "NAT", ticket_body(criteria))
        ticket_id = self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?",
            (identifier,)).fetchone()[0]
        return identifier, ticket_id

    def rows(self, ticket_id):
        return self.conn.execute(
            "SELECT criterion, position, text, witnessKind, witnessTarget,"
            " status, source FROM ticketCriteria WHERE ticketId = ? ORDER BY id",
            (ticket_id,)).fetchall()

    def current(self, ticket_id):
        return {row[0]: row for row in self.rows(ticket_id)}

    def test_native_filing_records_order_and_each_witness_kind(self):
        _, ticket_id = self.file()
        self.assertEqual(self.rows(ticket_id), [
            (1, 1, CRITERIA[0], "test", "tests/test_store_board.py", "open", "filed"),
            (2, 2, CRITERIA[1], "verify", "ruff check .", "open", "filed"),
            (3, 3, CRITERIA[2], "reviewer_read", None, "open", "filed"),
        ])

    def test_edits_keep_ids_history_and_json_then_retire_without_reusing(self):
        identifier, ticket_id = self.file()
        original = self.rows(ticket_id)
        added = "The data reference names the witness column."
        reworded = "The data reference explains the criterion column."
        revised = [CRITERIA[0], added, CRITERIA[1], f"**C3** {reworded}"]
        revision = store.board.edit_ticket(
            self.conn, self.project_id, identifier, ticket_body(revised),
            expected_revision=1)
        current = self.current(ticket_id)
        self.assertEqual({n: row[1:3] for n, row in current.items()}, {
            1: (1, CRITERIA[0]), 4: (2, added),
            2: (3, CRITERIA[1]), 3: (4, reworded),
        })
        self.assertEqual(self.rows(ticket_id)[:3], original)
        self.assertEqual(json.loads(self.conn.execute(
            "SELECT acceptanceCriteria FROM tickets WHERE id = ?",
            (ticket_id,)).fetchone()[0]), revised)
        without_second = [CRITERIA[0], added, f"**C3** {reworded}"]
        revision = store.board.edit_ticket(
            self.conn, self.project_id, identifier, ticket_body(without_second),
            expected_revision=revision)
        retired = self.current(ticket_id)[2]
        self.assertEqual((retired[1], retired[5]), (None, "retired"))
        new_text = "The data reference names the history table."
        store.board.edit_ticket(
            self.conn, self.project_id, identifier,
            ticket_body(without_second + [new_text]), expected_revision=revision)
        self.assertEqual(self.current(ticket_id)[5][1:3], (4, new_text))
        self.assertEqual(self.current(ticket_id)[2], retired)

    def test_repeated_and_empty_mirrors_leave_criterion_history_unchanged(self):
        _, ticket_id = self.file()
        original = self.rows(ticket_id)
        for criteria in (CRITERIA, []):
            store.tickets.mirror_ticket(
                self.conn, self.project_id, linear_issue_id="NAT-1",
                linear_identifier="NAT-1", title="Mirrored ticket",
                acceptance_criteria=criteria, verification_commands=[COMMANDS])
            self.assertEqual(self.rows(ticket_id), original)

    def test_historical_text_recovers_id_and_duplicate_prefix_cannot_take_it(self):
        identifier, ticket_id = self.file()
        revision = store.board.edit_ticket(
            self.conn, self.project_id, identifier,
            ticket_body([CRITERIA[0], CRITERIA[1], "**C3** Revised wording."]),
            expected_revision=1)
        store.board.edit_ticket(
            self.conn, self.project_id, identifier,
            ticket_body([CRITERIA[0], CRITERIA[1], CRITERIA[2],
                         "**C3** Another criterion."]),
            expected_revision=revision)
        current = self.current(ticket_id)
        self.assertEqual(current[3][1:3], (3, CRITERIA[2]))
        self.assertEqual(current[4][1:3], (4, "Another criterion."))

    def test_ready_filing_refuses_unwitnessed_operator_prose_only(self):
        text = "After the merge, the operator sees the new column"
        with self.assertRaises(store.board.FilingRefused) as refused:
            self.file([text])
        self.assertRegex(str(refused.exception), r"criterion 1")
        self.assertRegex(str(refused.exception), r"post-merge|operator")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tickets")
                         .fetchone()[0], 0)
        problems = store.board.ticket_problems(ticket_body([text]), None)
        self.assertTrue(any("criterion 1" in problem and
                            ("post-merge" in problem or "operator" in problem)
                            for problem in problems))
        for witnessed in (text + " (in `tests/test_store_board.py`)",
                          text + " (verified by `ruff check .`)",
                          "The column is named `operator`.",
                          "The column is named `` `operator` ``.",
                          "The column is named ``` ``operator`` ```."):
            with self.subTest(criterion=witnessed):
                _, ticket_id = self.file([witnessed])
                self.assertEqual(self.current(ticket_id)[1][5], "open")

    def test_ready_filing_keeps_operator_prose_outside_complete_code_spans(self):
        for text in ("The column is named ``operator`.",
                     "The column is named `operator``.",
                     "The column is named `` `value` `` by the operator."):
            with self.subTest(criterion=text):
                with self.assertRaises(store.board.FilingRefused) as refused:
                    self.file([text])
                self.assertRegex(
                    str(refused.exception),
                    r"criterion 1 reads as a post-merge or operator witness")

    def test_previous_version_backfills_only_open_tickets_once(self):
        self.conn.close()
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TABLE ticketCriteria")
        for identifier, status in (("OLD-1", "ready"), ("OLD-2", "merged"),
                                   ("OLD-3", "abandoned")):
            conn.execute(
                "INSERT INTO tickets (projectId, linearIssueId, linearIdentifier,"
                " title, status, acceptanceCriteria, verificationCommands,"
                " mirroredAt, affinity) VALUES (?, ?, ?, 'Old ticket', ?, ?, ?,"
                " 1, 'any')", (self.project_id, identifier, identifier, status,
                               json.dumps(CRITERIA), json.dumps([COMMANDS])))
        conn.execute(f"PRAGMA user_version = {store.schema.SCHEMA_VERSION - 1}")
        conn.commit()
        conn.close()
        self.conn = store.open(self.path)
        self.addCleanup(self.conn.close)
        rows = self.conn.execute(
            "SELECT t.linearIdentifier, c.criterion, c.position, c.text, c.source"
            " FROM ticketCriteria c JOIN tickets t ON t.id = c.ticketId"
            " ORDER BY c.id").fetchall()
        self.assertEqual(rows, [
            ("OLD-1", 1, 1, CRITERIA[0], "backfill"),
            ("OLD-1", 2, 2, CRITERIA[1], "backfill"),
            ("OLD-1", 3, 3, CRITERIA[2], "backfill"),
        ])
        self.conn.close()
        self.conn = store.open(self.path)
        self.addCleanup(self.conn.close)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ticketCriteria")
                         .fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
