"""An implementer that cannot meet a criterion as written declares it with a
`CONTRADICTION: criterion N — reason` line, and the run parks on a question
to the operator instead of going to review.

Fake agents script the turns; the repository, worktree, verify and store are real.
Run: python3 -m unittest discover -s tests -p 'test_implementer_contradiction.py'
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    REQUEST_CHANGES,
    Commit,
    Idle,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    LoopFixture,
    StubProvider,
    a_task,
)

import holophyte.board.projection  # noqa: E402 - after the sys.path insert above
import holophyte.cli.operator  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.board  # noqa: E402 - after the sys.path insert above
from holophyte.loop.contradiction import declared  # noqa: E402

# Spelled out rather than imported: the loop's parse is what is under test.
REASON = "the pr_open facts carry only open_threads"
DECLARED = f"CONTRADICTION: criterion 2 — {REASON}"


def two_criteria():
    return dict(a_task(), criteria=[
        "Given a parked run, when the page renders, then it says parked",
        "Given one bot thread, when the page renders, then it says"
        " 'parked · 1 bot thread'"])


class Declare(Commit):
    """An implement turn that commits a change and declares criterion 2
    unmeetable on its reply's last line."""

    def play(self, cwd, turn):
        return f"{super().play(cwd, turn)}\nCriterion 1 is done.\n{DECLARED}"


class DeclareTwo(Commit):
    """A fix turn that commits and declares criteria 1 and 3 unmeetable, the
    second line in markdown emphasis with a colon after the number."""

    def play(self, cwd, turn):
        return (f"{super().play(cwd, turn)}\n"
                "CONTRADICTION: criterion 1 — the facts lack a field\n"
                "- **CONTRADICTION:** Criterion 3: the file-size pin forbids it")


class DeclareCodex(Commit):

    def play(self, cwd, turn):
        message = (f"{super().play(cwd, turn)}\n"
                   "CONTRADICTION: criterion 1 — `tickets.parkKind` does not "
                   "exist; the reader reads `runs.parkKind`.")
        return f"codex\n{message}\ntokens used\n53,160\n{message}"


class DeclaredTests(unittest.TestCase):

    def test_whole_line_code_span_keeps_distinct_reasons_in_order(self):
        self.assertEqual(declared(
            "`CONTRADICTION: criterion 2 — the facts lack a field`\n"
            "CONTRADICTION: criterion 2 — the reader requires `parkKind`"), [
                (2, "the facts lack a field"),
                (2, "the reader requires `parkKind`"),
            ])


class ImplementTurnContradictionTests(LoopFixture):

    def test_codex_repeated_declaration_parks_once_with_intact_code_spans(self):
        self.loop(DeclareCodex("render the park kind"), REQUEST_CHANGES)

        ((question,),) = self.read("SELECT blockedQuestion FROM tickets")
        ((payload,),) = self.read(
            "SELECT payload FROM runEvents WHERE kind = 'contradiction'")
        with self.subTest("blocked question"):
            self.assertEqual(
                question.splitlines()[0],
                "implementer: criterion C1 cannot be met as written: "
                "`tickets.parkKind` does not exist; the reader reads `runs.parkKind`.")
        with self.subTest("event criteria"):
            self.assertEqual(json.loads(payload)["criteria"], [{
                "criterion": 1, "id": "C1",
                "reason": "`tickets.parkKind` does not exist; "
                          "the reader reads `runs.parkKind`.",
            }])

    def test_a_declared_contradiction_parks_on_the_operator_without_review(self):
        fake, _ = self.loop(Declare("render the parked word"), REQUEST_CHANGES,
                            Idle(), provider=StubProvider(two_criteria()))

        self.assertEqual(fake.roles, ["implement"])
        self.assertEqual(self.read("SELECT round FROM reviewRounds"), [])
        head = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(self.read(
            "SELECT phase, parkKind, candidateSha, outcome FROM runs"),
            [("blocked_on_operator", "contradiction", head, None)])
        ((status, question),) = self.read(
            "SELECT status, blockedQuestion FROM tickets")
        self.assertEqual(status, "blocked_on_operator")
        self.assertEqual(
            question.splitlines()[0],
            f"implementer: criterion C2 cannot be met as written: {REASON}")
        ((payload,),) = self.read(
            "SELECT payload FROM runEvents WHERE kind = 'contradiction'")
        self.assertEqual(json.loads(payload), {
            "turn": "implement", "head": head,
            "criteria": [{"criterion": 2, "id": "C2", "reason": REASON}]})
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        self.assertEqual(holophyte.board.projection.failure_history(conn, 1), [])


class RequeueAfterContradictionTests(LoopFixture):

    def test_requeue_releases_the_park_and_the_next_attempt_merges(self):
        self.loop(Declare("render the parked word"), REQUEST_CHANGES)

        holophyte.cli.operator.requeue(
            self.project, "KO-131", "criterion 2 now reads '1 thread open'",
            out=io.StringIO())

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("abandoned",)])
        self.assertEqual(self.read(
            "SELECT status, blockedQuestion FROM tickets"), [("ready", None)])

        fake, _ = self.loop(Commit("render the amended word"), APPROVE)

        self.assertIn("CONTRADICTION: criterion C1", fake.turns[0].goal)
        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertEqual(self.read("SELECT COUNT(*) FROM runEvents"
                                   " WHERE runId = 2 AND kind = 'contradiction'"),
                         [(0,)])
        self.assertEqual(self.read("SELECT status FROM tickets"), [("merged",)])
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("abandoned",), ("merged",)])
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        self.assertEqual(holophyte.board.projection.failure_history(conn, 1), [])


class CancelAfterContradictionTests(LoopFixture):
    """The question's third answer, cancel on the board, ends the parked run."""

    def test_a_native_board_cancel_ends_the_parked_run_abandoned(self):
        self.loop(Declare("render the parked word"), REQUEST_CHANGES)
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        ((project, revision),) = self.read(
            "SELECT projectId, revision FROM tickets")

        store.board.cancel_ticket(conn, project, "KO-131", revision,
                                  "the criterion is dropped")

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("abandoned",)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("abandoned",)])

    def test_a_linear_cancel_ends_the_parked_run_abandoned(self):
        provider = StubProvider(a_task())
        self.loop(Declare("render the parked word"), REQUEST_CHANGES,
                  provider=provider)
        provider.closed = {"KO-131": "canceled"}

        self.loop(provider=provider)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("abandoned",)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("abandoned",)])


class PauseDeclareTwo(DeclareTwo):
    """A fix turn that requests a cooperative pause before its reply lands."""

    def play(self, cwd, turn):
        conn = store.open(self.db)
        try:
            store.pause(conn, 1, "reboot writer")
        finally:
            conn.close()
        return super().play(cwd, turn)


class FixTurnContradictionTests(LoopFixture):

    def test_a_fix_turn_contradiction_parks_before_a_second_review(self):
        fake, _ = self.loop(Commit("render the parked word"), REQUEST_CHANGES,
                            DeclareTwo("address the finding"), APPROVE)

        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        self.assertEqual(self.read("SELECT round FROM reviewRounds"), [(1,)])
        self.assertEqual(self.read("SELECT phase, parkKind FROM runs"),
                         [("blocked_on_operator", "contradiction")])
        ((payload,),) = self.read(
            "SELECT payload FROM runEvents WHERE kind = 'contradiction'")
        payload = json.loads(payload)
        self.assertEqual(payload["turn"], "fix")
        self.assertEqual(payload["criteria"], [
            {"criterion": 1, "id": "C1", "reason": "the facts lack a field"},
            {"criterion": 3, "id": "C3", "reason": "the file-size pin forbids it"}])
        ((question,),) = self.read("SELECT blockedQuestion FROM tickets")
        first = question.splitlines()[0]
        self.assertIn("criterion C1 cannot be met as written: the facts lack"
                      " a field", first)
        self.assertIn("criterion C3 cannot be met as written: the file-size"
                      " pin forbids it", first)

    def test_a_pause_requested_in_the_fix_turn_still_parks_the_contradiction(self):
        fix = PauseDeclareTwo("address the finding")
        fix.db = str(self.db)

        fake, _ = self.loop(Commit("render the parked word"), REQUEST_CHANGES,
                            fix, APPROVE)

        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        self.assertEqual(self.read("SELECT phase, parkKind, outcome FROM runs"),
                         [("blocked_on_operator", "contradiction", None)])
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM runEvents WHERE kind = 'contradiction'"),
            [(1,)])


if __name__ == "__main__":
    unittest.main()
