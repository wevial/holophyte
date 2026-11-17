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
import store.read
import store.steer_notes
import store.tickets
from holophyte.host import supervisor
from holophyte.loop import dispatch, review_round, runs, steering
from holophyte.loop.gates import InfraFailure, RunFailure
from holophyte.serve.serve_findings import stop_kind
from tests.test_store_board import body

PROJECT = SimpleNamespace(config=lambda: {}, config_path="holophyte.toml")
EDITED = "echo edited"
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
        self.project = SimpleNamespace(path=self.root, config=lambda: {},
                                       config_path="holophyte.toml")
        key = store.board.file_ticket(self.conn, project, "NAT", TICKET)
        self.project_id, self.key = project, key
        self.ticket = self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?", (key,)
        ).fetchone()[0]
        store.tickets.transition(self.conn, self.ticket, "in_flight")
        self.run_id = store.claim(self.conn, project, self.ticket)
        store.set_phase(self.conn, self.run_id, "working")
        self.calls, self.fixes, self.trips = [], [], []

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, message):
        self.git("-c", "user.name=t", "-c", "user.email=t@example.test",
                 "commit", "-q", "--allow-empty", "-m", message)

    def drive(self, reviews, ruling, cap, criteria=CRITERIA):
        reviews = list(reviews)

        def agent(project, role, goal, cwd, **kwargs):
            self.calls.append((role, goal))
            if role == "adjudicate":
                if isinstance(ruling, Exception):
                    raise ruling
                return ruling
            return reviews.pop(0) if role == "review" else ""

        def captured(conn, run_id, sha, lines):
            self.trips.append(supervisor.review_trip(conn, run_id, 0.5))
            return capture_review(conn, run_id, sha, lines)

        capture_review = review_round.capture_review

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
                        " of scope\n"
                        "FOLLOW_UP(feature): page the legacy export")
            return "fixed", False

        with patch.object(review_round, "agent", side_effect=agent), \
                patch.object(runs, "agent_route", return_value="route"), \
                patch.object(review_round, "capture_review",
                             side_effect=captured), \
                patch("holophyte.agents.fix_session.fix_turn",
                      side_effect=fix_turn):
            head = self.git("rev-parse", "HEAD")
            return review_round._review_rounds(
                PROJECT, self.conn, self.run_id, None, "NAT-1", "task/nat-1",
                self.root, 60, "main", head, TICKET, "true", [], criteria, 10,
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
                     " scope",
                     "FOLLOW_UP(feature): page the legacy export"):
            self.assertIn(held, brief)
        self.assertNotIn("THREAD 3", brief)

    def test_a_sweep_once_the_repeating_round_is_recorded_does_not_trip(self):
        self.drive([review(ALPHA, BETA), review(ALPHA, BETA, GAMMA),
                    review(DELTA)],
                   "THREAD 1: FIX -- alpha blocks the criterion\n"
                   "THREAD 2: FIX -- beta blocks it too", cap=3, criteria=[])
        self.assertEqual(self.trips, [None, None, None])

    def test_fix_turn_gets_fix_and_new_findings_and_the_drop_becomes_a_note(self):
        self.drive([review(ALPHA, BETA), review(ALPHA, BETA, GAMMA),
                    review(DELTA)],
                   "THREAD 1: FIX -- alpha blocks the criterion\n"
                   "THREAD 2: DROP -- beta is out of scope", cap=3)
        handed = self.fixes[1]
        self.assertIn(ALPHA, handed)
        self.assertIn("- orders/export.py:1: FIX -- alpha blocks the criterion",
                      handed)
        self.assertIn(GAMMA, handed)
        self.assertNotIn("orders/legacy.py:2", handed)
        (note,) = store.steer_notes.chain_notes(self.conn, self.ticket, 0)
        self.assertEqual(note.author, "factory")
        self.assertIn("beta is out of scope", note.note)
        self.assertEqual(store.steer_notes.pending(self.conn, self.ticket), [])
        third_review = self.goals("review")[2]
        self.assertIn(f"steer note {note.id} by factory", third_review)
        self.assertIn("beta is out of scope", third_review)

    def test_a_drop_note_keeps_the_claimed_contract_and_the_strike_count(self):
        store.board.edit_ticket(self.conn, self.project_id, self.key,
                                TICKET.replace("\ntrue\n", f"\n{EDITED}\n"),
                                expected_revision=1)
        self.drive([review(ALPHA, BETA), review(ALPHA, BETA, GAMMA),
                    review(DELTA)],
                   "THREAD 1: FIX -- alpha blocks the criterion\n"
                   "THREAD 2: DROP -- beta is out of scope", cap=3)
        self.assertEqual(steering.adopted_verify(self.conn, self.run_id, "true"),
                         "true")
        self.assertEqual(store.read.latest_human_intervention_at(
            self.conn, self.ticket), 0)

    def test_dropping_every_repeated_finding_reviews_again_without_a_fix_turn(self):
        self.drive([review(ALPHA), review(ALPHA), review(DELTA)],
                   "THREAD 1: DROP -- alpha was answered", cap=3,
                   criteria=[])
        self.assertEqual(self.roles(), ["review", "fix", "review", "adjudicate",
                                        "review", "fix"])
        self.assertIn("alpha was answered", self.goals("review")[2])

    def stuck(self, reviews, ruling, fixes, cap=3):
        with self.assertRaises(RunFailure) as failed:
            self.drive(reviews, ruling, cap=cap)
        error = failed.exception
        self.assertEqual(len(self.fixes), fixes)
        dispatch.fail_run(self.project, self.conn, self.run_id, self.ticket,
                          None, error.reason, "work", error.failure_kind,
                          refresh=False)
        outcome, kind, reason = self.conn.execute(
            "SELECT outcome, failureKind, outcomeReason FROM runs WHERE id = ?",
            (self.run_id,)).fetchone()
        self.assertEqual((outcome, kind), ("failed", "swept"))
        self.assertTrue(reason.startswith("review_stuck (rounds "), reason)
        self.assertIn("worktree preserved", reason)
        (event,) = self.conn.execute(
            "SELECT kind, summary FROM runEvents WHERE runId = ? AND kind = ?",
            (self.run_id, supervisor.SWEEP_EVENT)).fetchall()
        self.assertEqual(stop_kind(*event), "review_stuck")
        self.assertEqual(self.git("branch", "--show-current"), "task/nat-1")
        self.assertEqual(self.git("status", "--porcelain", "orders"), "")
        self.assertEqual(store.steer_notes.chain_notes(
            self.conn, self.ticket, 0), [])
        return reason

    def test_a_human_ruling_ends_the_run_stuck_with_the_rulings(self):
        reason = self.stuck([review(ALPHA, BETA), review(ALPHA, BETA, GAMMA)],
                            "THREAD 1: FIX -- alpha blocks the criterion\n"
                            "THREAD 2: HUMAN -- the ticket does not say",
                            fixes=1)
        self.assertIn("FIX -- alpha blocks the criterion", reason)
        self.assertIn("HUMAN -- the ticket does not say", reason)

    def test_a_missing_or_conflicting_ruling_ends_the_run_stuck(self):
        reason = self.stuck([review(ALPHA, BETA), review(ALPHA, BETA, GAMMA)],
                            "THREAD 1: HUMAN -- the ticket does not say\n"
                            "THREAD 1: FIX -- alpha blocks the criterion\n"
                            "THREAD 2: DROP --", fixes=1)
        self.assertIn("orders/export.py:1 HUMAN -- the adjudicator gave"
                      " conflicting rulings", reason)
        self.assertIn("orders/legacy.py:2 no ruling", reason)

    def test_a_failed_adjudicator_turn_ends_the_run_stuck(self):
        reason = self.stuck([review(ALPHA, BETA), review(ALPHA, BETA, GAMMA)],
                            InfraFailure("the container did not start",
                                         "review_route"), fixes=1)
        self.assertIn("the adjudicator turn failed: the container did not"
                      " start", reason)

    def test_a_second_repeat_after_an_adjudication_ends_the_run_stuck(self):
        reason = self.stuck([review(ALPHA, BETA), review(ALPHA, BETA, GAMMA),
                             review(ALPHA, GAMMA)],
                            "THREAD 1: FIX -- alpha blocks the criterion\n"
                            "THREAD 2: FIX -- beta blocks it too", fixes=2,
                            cap=4)
        self.assertIn("already ruled on round 2", reason)
        self.assertIn("FIX -- alpha blocks the criterion", reason)
        self.assertIn("FIX -- beta blocks it too", reason)
        self.assertEqual(self.roles().count("adjudicate"), 1)


if __name__ == "__main__":
    unittest.main()
