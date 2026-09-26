"""KO-754: `[board] kind = "native"` builds a `NativeBoard`, whose members
answer from the project's store: filing and the body read-back go through
`store.board`, `states()` reads the rows, the lease writes do nothing and a
comment is a note. Nothing asks Linear. A real store under a throwaway home.

Run: python3 -m unittest discover -s tests -p 'test_native_board.py' -v
"""
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert

import linear_provider  # noqa: E402
from holophyte.native_board import NativeBoard  # noqa: E402
from provider import GONE, board_for  # noqa: E402
from tests.test_provider import ticket_body  # noqa: E402

NATIVE = '[board]\nkind = "native"\nkey = "NAT"\n'


def body(title, estimate=20):
    """A body filing takes: the fixture's, its verify line one module."""
    return ticket_body(title=title, estimate=estimate, verify=(
        "python3 -m unittest discover -s tests -p 'test_thing.py'"))


def no_linear(*args, **kwargs):
    raise AssertionError("a native board asked Linear")


class NativeBoardTests(ConfigTestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.board = board_for(self.locate(NATIVE))
        (self.target / "tests").mkdir()
        (self.target / "tests" / "test_thing.py").write_text("")

    def sql(self, statement, *params):
        conn = sqlite3.connect(self.project.store_path)
        try:
            with conn:
                return conn.execute(statement, params).fetchall()
        finally:
            conn.close()

    def test_a_filed_ticket_reads_back_from_the_store(self):
        body_text = body("Native thing", estimate=25)
        self.assertIsInstance(self.board, NativeBoard)
        self.assertEqual(self.board.team, "native:NAT")

        self.assertEqual(self.board.file("Native thing", body_text, 25, "Todo"),
                         "NAT-1")
        self.assertEqual(self.board.stored_body("NAT-1"), body_text)
        task = self.board.fetch_task("NAT-1")
        self.assertEqual(task["title"], "Native thing")
        self.assertEqual(task["column"], "ready")
        self.assertEqual(task["budget_min"], 25)
        self.assertIsNone(self.board.fetch_task("NAT-9"))

    def file_three(self):
        """NAT-1 and NAT-2 in Todo, NAT-3 in Backlog."""
        for title, state in (("One", "Todo"), ("Two", "Todo"),
                             ("Three", "Backlog")):
            self.board.file(title, body(title), 20, state)

    def test_states_answer_merged_canceled_open_and_gone(self):
        self.file_three()
        self.sql("UPDATE tickets SET status = 'merged'"
                 " WHERE linearIdentifier = 'NAT-1'")
        self.sql("UPDATE tickets SET boardColumn = 'canceled'"
                 " WHERE linearIdentifier = 'NAT-2'")
        asked = ["NAT-1", "NAT-2", "NAT-3", "NAT-9"]

        states = self.board.states(asked)
        self.assertEqual(
            {i: (s["state"], s["column"]) for i, s in states.items()},
            {"NAT-1": ("completed", None), "NAT-2": ("canceled", "canceled"),
             "NAT-3": ("open", "backlog"), "NAT-9": (GONE, None)})
        self.assertEqual(self.board.closed_identifiers(asked),
                         {"NAT-1": "completed", "NAT-2": "canceled"})

    def test_a_lease_label_is_not_held_and_a_comment_is_one_note(self):
        self.file_three()
        labels = "SELECT labels FROM tickets WHERE linearIdentifier = 'NAT-3'"
        before = self.sql(labels)

        self.board.label_issue("NAT-3", "holo:writer")
        self.assertEqual(self.board.issue_labels("NAT-3"), [])
        self.board.comment("NAT-3", "text")

        self.assertEqual(self.sql(labels), before)
        self.assertEqual(self.sql(
            "SELECT t.linearIdentifier, n.kind, n.text FROM ticketNotes n"
            " JOIN tickets t ON t.id = n.ticketId"), [("NAT-3", "comment", "text")])
