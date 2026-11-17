"""A review that repeats its findings goes to the adjudicator once per run.

The review rounds run for real against a temporary git repository and store;
only the reviewer, the adjudicator and the fix turn are faked, the fix turn
committing its ADDRESS and DECLINE lines as a real implementer would.

Run: python3 -m unittest discover -s tests -p 'test_stuck_review*.py'
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import store
import store.board
import store.steer_notes
import store.tickets
from holophyte.loop import review_round, runs
from holophyte.loop.gates import RunFailure
from tests.test_store_board import body

PROJECT = SimpleNamespace(config=lambda: {}, config_path="holophyte.toml")
CRITERIA = ["the export lists every order"]
TICKET = body().replace(
    "- CSV serialization of the orders list", "- `orders/export.py`").replace(
    "- Excel-specific formatting", "- `orders/legacy.py`").replace(
    "- [ ] Given 3 orders, when GET /orders.csv, then 4 lines including"
    " header.", "- [ ] the export lists every order").replace(
    "python3 -m unittest test_orders_export", "true")
ALPHA = "- orders/export.py:1 [p1] [correctness] alpha drops the last order"
BETA = "- orders/legacy.py:2 [p1] [correctness] beta breaks the legacy path"
GAMMA = "- orders/export.py:3 [p1] [correctness] gamma skips a header"
DELTA = "- orders/export.py:4 [p1] [correctness] delta misnames a column"


def review(*findings):
    return "\n".join(findings) + "\nVERDICT: REQUEST_CHANGES"


class StuckReviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "orders").mkdir()
        (self.root / "orders/export.py").write_text("ORDERS = []\n")
        self.git("init", "-q", "-b", "main")
        self.git("add", "orders")
        self.commit("base")
        self.git("checkout", "-q", "-b", "task/nat-1")
        self.conn = store.open(self.root / "store.db")
        self.addCleanup(self.conn.close)
        project = store.tickets.ensure_project(
            self.conn, "native-team", directory.name)
        key = store.board.file_ticket(self.conn, project, "NAT", TICKET)
        self.ticket = self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?", (key,)
        ).fetchone()[0]
        store.tickets.transition(self.conn, self.ticket, "in_flight")
        self.run_id = store.claim(self.conn, project, self.ticket)
        store.set_phase(self.conn, self.run_id, "working")
        self.calls, self.fixes = [], []

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, message):
        self.git("-c", "user.name=t", "-c", "user.email=t@example.test",
                 "commit", "-q", "--allow-empty", "-m", message)

    def drive(self, reviews, ruling, cap):
        reviews = list(reviews)

        def agent(project, role, goal, cwd, **kwargs):
            self.calls.append((role, goal))
            if role == "adjudicate":
                return ruling
            return reviews.pop(0) if role == "review" else ""

        def fix_turn(project, conn, run_id, beat_s, wt, budget_min, ticket,
                     findings, sha, **kwargs):
            self.calls.append(("fix", findings))
            self.fixes.append(findings)
            (self.root / "orders/export.py").write_text(
                f"ORDERS = [{len(self.fixes)}]\n")
            self.git("add", "orders")
            self.commit(f"fix round {len(self.fixes)}\n\n"
                        "ADDRESS: orders/export.py:1 keeps the last order\n"
                        "DECLINE: orders/legacy.py:2 the legacy path is out"
                        " of scope")
            return "fixed", False

        with patch.object(review_round, "agent", side_effect=agent), \
                patch.object(runs, "agent_route", return_value="route"), \
                patch("holophyte.agents.fix_session.fix_turn",
                      side_effect=fix_turn):
            head = self.git("rev-parse", "HEAD")
            return review_round._review_rounds(
                PROJECT, self.conn, self.run_id, None, "NAT-1", "task/nat-1",
                self.root, 60, "main", head, TICKET, "true", [], CRITERIA, 10,
                cap)

    def roles(self):
        return [role for role, _ in self.calls]

    def goals(self, role):
        return [goal for each, goal in self.calls if each == role]

    def test_adjudicator_rules_once_before_the_fix_turn_with_the_dispute(self):
        self.drive([review(ALPHA, BETA), review(ALPHA, BETA, GAMMA),
                    review(DELTA)],
                   "THREAD 1: FIX -- alpha blocks the criterion\n"
                   "THREAD 2: DROP -- beta is out of scope", cap=3)
        self.assertEqual(self.roles(), ["review", "fix", "review", "adjudicate",
                                        "fix", "review", "fix"])
        (brief,) = self.goals("adjudicate")
        for held in ("`orders/export.py`", "`orders/legacy.py`",
                     "C1. the export lists every order",
                     "THREAD 1 -- orders/export.py:1 [p1]\n"
                     "- orders/export.py:1 alpha drops the last order",
                     "THREAD 2 -- orders/legacy.py:2 [p1]\n"
                     "- orders/legacy.py:2 beta breaks the legacy path",
                     "ADDRESS: orders/export.py:1 keeps the last order",
                     "DECLINE: orders/legacy.py:2 the legacy path is out of"
                     " scope"):
            self.assertIn(held, brief)
        self.assertNotIn("THREAD 3", brief)

    def test_fix_turn_gets_fix_and_new_findings_and_the_drop_becomes_a_note(self):
        self.drive([review(ALPHA, BETA), review(ALPHA, BETA, GAMMA),
                    review(DELTA)],
                   "THREAD 1: FIX -- alpha blocks the criterion\n"
                   "THREAD 2: DROP -- beta is out of scope", cap=3)
        handed = self.fixes[1]
        self.assertIn("alpha drops the last order\n"
                      "  Adjudicator ruling: FIX -- alpha blocks the criterion",
                      handed)
        self.assertIn("orders/export.py:3 gamma skips a header", handed)
        self.assertIn("CRITERION C1: unwitnessed", handed)
        self.assertNotIn("orders/legacy.py:2", handed)
        (note,) = store.steer_notes.amendments(self.conn, self.ticket)
        self.assertEqual(note.author, "factory")
        self.assertIn("beta is out of scope", note.note)
        self.assertEqual(store.steer_notes.pending(self.conn, self.ticket), [])
        third_review = self.goals("review")[2]
        self.assertIn(f"steer note {note.id} by factory", third_review)
        self.assertIn("beta is out of scope", third_review)

    def assert_stuck(self, failed, fixes):
        reason = str(failed.exception)
        self.assertTrue(reason.startswith("review_stuck (rounds "), reason)
        self.assertIn("worktree preserved", reason)
        self.assertEqual(len(self.fixes), fixes)
        self.assertEqual(self.git("branch", "--show-current"), "task/nat-1")
        self.assertTrue((self.root / "orders/export.py").is_file())
        return reason

    def test_a_human_ruling_ends_the_run_stuck_with_the_rulings(self):
        with self.assertRaises(RunFailure) as failed:
            self.drive([review(ALPHA, BETA), review(ALPHA, BETA, GAMMA)],
                       "THREAD 1: FIX -- alpha blocks the criterion\n"
                       "THREAD 2: HUMAN -- the ticket does not say", cap=3)
        reason = self.assert_stuck(failed, fixes=1)
        self.assertIn("FIX -- alpha blocks the criterion", reason)
        self.assertIn("HUMAN -- the ticket does not say", reason)
        self.assertEqual(store.steer_notes.amendments(self.conn, self.ticket),
                         [])

    def test_a_repeated_finding_left_without_a_ruling_ends_the_run_stuck(self):
        with self.assertRaises(RunFailure) as failed:
            self.drive([review(ALPHA, BETA), review(ALPHA, BETA, GAMMA)],
                       "THREAD 1: DROP -- alpha was answered", cap=3)
        reason = self.assert_stuck(failed, fixes=1)
        self.assertIn("orders/legacy.py:2 no ruling", reason)
        self.assertEqual(store.steer_notes.amendments(self.conn, self.ticket),
                         [])

    def test_a_second_repeat_after_an_adjudication_ends_the_run_stuck(self):
        with self.assertRaises(RunFailure) as failed:
            self.drive([review(ALPHA, BETA), review(ALPHA, BETA, GAMMA),
                        review(ALPHA, GAMMA)],
                       "THREAD 1: FIX -- alpha blocks the criterion\n"
                       "THREAD 2: FIX -- beta blocks it too", cap=4)
        reason = self.assert_stuck(failed, fixes=2)
        self.assertIn("already ruled on round 2", reason)
        self.assertEqual(self.roles().count("adjudicate"), 1)


if __name__ == "__main__":
    unittest.main()
