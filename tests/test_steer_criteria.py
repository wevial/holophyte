import tempfile
import unittest
from pathlib import Path

import store
import store.board
import store.steer_notes
import store.tickets
from holophyte.babysit.maintainer_notes import PREFIX, carry_amendments
from holophyte.board.projection import mirror_task
from holophyte.loop import steering
from holophyte.review.criterion_ids import numbered
from provider import parse_body
from tests.test_ticket_criteria_rows import ticket_body

FILED = ["the list shows every entry", "the list is sorted by name",
         "the list labels each age"]


class SteerCriteriaTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = store.open(Path(directory.name) / "store.db")
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, "native-team", directory.name)
        self.key = store.board.file_ticket(
            self.conn, self.project_id, "NAT", ticket_body(FILED))
        self.ticket = self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?", (self.key,)
        ).fetchone()[0]

    def amend(self, text, **kwargs):
        return store.steer_notes.steer(
            self.conn, self.ticket, text, "maintainer", **kwargs)

    def claim(self):
        store.tickets.transition(self.conn, self.ticket, "in_flight")
        return store.claim(self.conn, self.project_id, self.ticket)

    def test_replacement_preserves_filed_history_and_review_id(self):
        note = self.amend("REPLACE C2: the list is sorted by age")
        rows = self.conn.execute(
            "SELECT text, steerNoteId FROM ticketCriteria"
            " WHERE ticketId = ? AND criterion = 2 ORDER BY id", (self.ticket,)
        ).fetchall()
        self.assertEqual(rows, [("the list is sorted by name", None),
                                ("the list is sorted by age", note.id)])
        run = self.claim()
        expected = [(1, FILED[0]), (2, "the list is sorted by age"), (3, FILED[2])]
        self.assertEqual(numbered(self.conn, run, FILED), expected)
        self.assertEqual(numbered(self.conn, run, list(enumerate(FILED, 1))),
                         expected)

    def test_drop_add_and_withdraw_restore_filed_text_without_reusing_ids(self):
        self.amend("REPLACE C2: the list is sorted by age")
        note = self.amend("DROP C3: covered by C2\n"
                          "ADD: the empty list shows a placeholder line")
        self.assertEqual(self.conn.execute(
            "SELECT criterion, status FROM ticketCriteria"
            " WHERE steerNoteId = ? ORDER BY id", (note.id,)).fetchall(),
            [(3, "retired"), (4, "open")])
        run = self.claim()
        self.assertEqual(numbered(self.conn, run, FILED), [
            (1, FILED[0]), (2, "the list is sorted by age"),
            (4, "the empty list shows a placeholder line")])
        store.release(self.conn, run, "failed", "retry")
        store.steer_notes.withdraw(self.conn, self.ticket, "restore the contract")
        self.assertEqual(numbered(self.conn, run, FILED), list(enumerate(FILED, 1)))
        self.amend("ADD: the list displays a count")
        self.assertEqual(numbered(self.conn, run, FILED),
                         list(enumerate(FILED, 1)) + [(5, "the list displays a count")])

    def test_invalid_edits_roll_back_the_entire_note_and_its_rows(self):
        for text, hint, reason in (
            ("REPLACE C9: x", False, "C9"),
            ("DROP C1", True, "hint"),
            ("ADD: After the merge, the operator checks the page", False,
             "test file or verify command"),
            ("ADD: a count is shown\nREPLACE C9: x", False, "C9"),
        ):
            with self.subTest(text=text, hint=hint):
                before = list(self.conn.iterdump())
                with self.assertRaisesRegex(store.steer_notes.SteerRefused, reason):
                    self.amend(text, hint=hint)
                self.assertEqual(list(self.conn.iterdump()), before)

    def test_edit_verbs_in_ordinary_prose_are_carried_unchanged(self):
        text = "Replace cache storage\nDrop caching for now\nAdd a count"
        note = self.amend(text)
        rendered = carry_amendments(self.conn, self.claim(), "Filed ticket body")
        self.assertEqual(rendered, "Filed ticket body\n\n" + PREFIX
                         + f"\nsteer note {note.id} by maintainer:\n" + text)

    def test_replacement_rederives_witness_and_add_accepts_verify_witness(self):
        note = self.amend(
            "replace c2: the list is sorted in `tests/test_store_board.py`\n"
            "add: After the merge, the operator checks `ruff check .`")
        self.assertEqual(self.conn.execute(
            "SELECT criterion, witnessKind, witnessTarget FROM ticketCriteria"
            " WHERE steerNoteId = ? ORDER BY id", (note.id,)).fetchall(), [
                (2, "test", "tests/test_store_board.py"),
                (4, "verify", "ruff check .")])

    def test_malformed_edit_commands_refuse_without_writing(self):
        for line in ("REPLACE C0: new text", "REPLACE C2:", "replace c2:   ",
                     "REPLACE C2 new text", "DROP C0", "DROP C2 extra", "ADD:"):
            with self.subTest(line=line):
                before = list(self.conn.iterdump())
                with self.assertRaisesRegex(store.steer_notes.SteerRefused,
                                            "malformed criterion edit"):
                    self.amend("ADD: the list displays a count\n" + line)
                self.assertEqual(list(self.conn.iterdump()), before)

    def test_carry_keeps_filed_position_order_and_lists_drops_by_id(self):
        revised = ["**C4** the list displays a count", "**C2** " + FILED[1],
                   "**C3** " + FILED[2], "**C1** " + FILED[0]]
        store.board.edit_ticket(self.conn, self.project_id, self.key,
                                ticket_body(revised), expected_revision=1)
        self.amend("REPLACE C2: the list is sorted by age\n"
                   "DROP C3: covered by C2\nDROP C1: covered by C4")
        rendered = carry_amendments(self.conn, self.claim(), "Filed ticket body")
        self.assertEqual(rendered.split("Amended acceptance criteria:\n")[1],
                         "- **C4** the list displays a count\n"
                         "- **C2** the list is sorted by age\n"
                         "- C1 dropped: covered by C4\n"
                         "- C3 dropped: covered by C2")

    def test_carry_renders_one_amended_block_and_only_free_note_lines(self):
        note = self.amend("REPLACE C2: the list is sorted by age\n"
                          "DROP C3: covered by C2\nKeep the old flag name")
        rendered = carry_amendments(self.conn, self.claim(), "Filed ticket body")
        self.assertTrue(rendered.startswith("Filed ticket body"))
        self.assertEqual(rendered.count("**C1**"), 1)
        self.assertEqual(rendered.count("**C2**"), 1)
        self.assertRegex(rendered, r"\*\*C1\*\*[^\n]*the list shows every entry")
        self.assertRegex(rendered, r"\*\*C2\*\*[^\n]*the list is sorted by age")
        self.assertRegex(rendered, r"C3[^\n]*[Dd]ropped[^\n]*covered by C2")
        self.assertIn(f"{PREFIX}\nsteer note {note.id} by maintainer:\n"
                      "Keep the old flag name", rendered)
        self.assertNotIn("REPLACE C2:", rendered)
        self.assertNotIn("DROP C3:", rendered)
        self.assertNotIn("the list is sorted by name", rendered)

    def test_live_edits_render_once_at_opening_and_after_delivery(self):
        run = self.claim()
        note = self.amend("REPLACE C2: the list is sorted by age\n"
                          "DROP C3: covered by C2\nKeep the old flag name")
        ticket = carry_amendments(self.conn, run, "Filed ticket body")
        opening = ticket + steering.take(self.conn, run)
        reviewed = steering.reviewed_ticket(self.conn, run, ticket)
        for rendered in (ticket, opening, reviewed):
            with self.subTest(rendered=rendered):
                self.assertEqual(rendered.count("Amended acceptance criteria:"), 1)
                self.assertIn("**C1** the list shows every entry", rendered)
                self.assertIn("**C2** the list is sorted by age", rendered)
                self.assertIn("C3 dropped: covered by C2", rendered)
                self.assertEqual(rendered.count("Keep the old flag name"), 1)
                self.assertIn(f"{PREFIX}\nsteer note {note.id} by maintainer:",
                              rendered)
                self.assertNotIn("REPLACE C2:", rendered)
                self.assertNotIn("DROP C3:", rendered)
        self.amend("REPLACE C2: the list is sorted by score\nKeep the flag short")
        delivered = steering.take(self.conn, run)
        reviewed = steering.reviewed_ticket(self.conn, run, ticket)
        for rendered in (delivered, reviewed):
            self.assertEqual(rendered.count("Amended acceptance criteria:"), 1)
            self.assertIn("**C2** the list is sorted by score", rendered)
            self.assertIn("Keep the flag short", rendered)
            self.assertIn("C3 dropped: covered by C2", rendered)
            self.assertNotIn("REPLACE C2:", rendered)
            self.assertNotIn("the list is sorted by age", rendered)
        self.assertFalse(steering.pending(self.conn, run))

    def test_replacement_matching_another_criterion_text_keeps_both_ids(self):
        self.amend("REPLACE C2: the list shows every entry")
        run = self.claim()
        self.assertEqual(numbered(self.conn, run, FILED), [
            (1, "the list shows every entry"),
            (2, "the list shows every entry"), (3, FILED[2])])

    def test_body_update_supersedes_replacement_even_after_withdrawal(self):
        self.amend("REPLACE C2: the list is sorted by age")
        revised = [FILED[0], "**C2** the list is sorted by score", FILED[2]]
        store.board.edit_ticket(self.conn, self.project_id, self.key,
                                ticket_body(revised), expected_revision=1)
        run = self.claim()
        expected = [(1, FILED[0]), (2, "the list is sorted by score"), (3, FILED[2])]
        self.assertEqual(numbered(self.conn, run, revised), expected)
        store.release(self.conn, run, "failed", "retry")
        store.steer_notes.withdraw(self.conn, self.ticket, "restore the contract")
        self.assertEqual(numbered(self.conn, run, revised), expected)

    def test_unchanged_board_refresh_preserves_amendments_through_claim(self):
        self.amend("REPLACE C2: the list is sorted by age\n"
                   "DROP C3: covered by C2\n"
                   "ADD: the empty list shows a placeholder line")
        rows = self.conn.execute("SELECT * FROM ticketCriteria").fetchall()
        task = parse_body(self.key, ticket_body(FILED))
        mirror_task(self.conn, self.project_id, task)
        run = self.claim()
        mirror_task(self.conn, self.project_id, task)
        self.assertEqual(numbered(self.conn, run, FILED), [
            (1, FILED[0]), (2, "the list is sorted by age"),
            (4, "the empty list shows a placeholder line")])
        self.assertEqual(self.conn.execute("SELECT * FROM ticketCriteria").fetchall(),
                         rows)
        rendered = carry_amendments(self.conn, run, "Filed ticket body")
        self.assertIn("**C2** the list is sorted by age", rendered)
        self.assertIn("**C4** the empty list shows a placeholder line", rendered)
        self.assertRegex(rendered, r"C3[^\n]*[Dd]ropped[^\n]*covered by C2")
