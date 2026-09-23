"""A reported defect the implementer could not reproduce parks on the
maintainer instead of burning review rounds and a strike (KO-657).

The implementer ends its reply with `OUTCOME: NOT_REPRODUCED`; the loop
verifies, then puts one evidence check to the adjudicate seat as round 1.
PASS parks the run `not_reproduced`; FAIL is a normal round with a fix turn.
Fake agents script the turns; the repository, worktree, verify and store are real.
Run: python3 -m unittest tests.test_not_reproduced -v
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    REQUEST_CHANGES,
    Commit,
    FakeAgent,
    Idle,
    Reply,
    no_agent_processes,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    LoopFixture,
    MergeModeFixture,
    StubProvider,
    a_task,
)

import holophyte.board  # noqa: E402 - after the sys.path insert above
import holophyte.loop  # noqa: E402 - after the sys.path insert above
import holophyte.operator  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
from holophyte.gates import MergeParked  # noqa: E402 - after the sys.path insert
from holophyte.stop import command  # noqa: E402 - after the sys.path insert above

# Spelled out rather than imported: the loop's parse is what is under test.
DECLARED = "OUTCOME: NOT_REPRODUCED"
REPRODUCED = Reply("The new test opens the rename-guest modal from the real"
                   " guest record; only tests changed.\nVERDICT: PASS")
REASON = "The added test never opens the rename-guest modal."
REFUSED = Reply(f"{REASON}\nVERDICT: FAIL")


class Declare(Commit):
    """An implementer turn that commits a test and declares the report not
    reproduced on its reply's last line."""

    def play(self, cwd, turn):
        return f"{super().play(cwd, turn)}\nThe test passes on main.\n{DECLARED}"


def pause_live_run(db, note):
    """Ask for a cooperative stop of the open run, as `--pause` does."""
    conn = store.open(str(db))
    try:
        (run,) = conn.execute("SELECT id FROM runs WHERE endedAt IS NULL"
                              " ORDER BY id DESC LIMIT 1").fetchone()
        store.pause(conn, run, note)
    finally:
        conn.close()


class DeclareThenPause(Declare):
    """The declaring implement turn, with a pause requested while it ran."""

    def __init__(self, db):
        super().__init__("test the modal")
        self.db = db

    def play(self, cwd, turn):
        pause_live_run(self.db, "reboot writer")
        return super().play(cwd, turn)


class RefuseThenPause(Reply):
    """The evidence check's FAIL, with a pause requested while it ran."""

    def __init__(self, db):
        super().__init__(REFUSED.text)
        object.__setattr__(self, "db", db)

    def play(self, cwd, turn):
        pause_live_run(self.db, "review checkpoint")
        return super().play(cwd, turn)


class NotReproducedTests(LoopFixture):

    def assert_parked_on_maintainer(self):
        head = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(self.read(
            "SELECT phase, parkKind, candidateSha, outcome FROM runs"),
            [("awaiting_merge_approval", "not_reproduced", head, None)])
        ((status, question),) = self.read(
            "SELECT status, blockedQuestion FROM tickets")
        self.assertEqual(status, "blocked_on_operator")
        first = question.splitlines()[0]
        self.assertTrue(first.startswith("not reproduced:"), first)
        self.assertIn(head[:12], first)
        self.assertIn(self.base[:12], first)
        self.assertIn("--approve", question)
        self.assertIn("--requeue", question)
        ((payload,),) = self.read(
            "SELECT payload FROM runEvents WHERE kind = 'not_reproduced'")
        self.assertEqual(json.loads(payload),
                         {"base": self.base, "candidate": head})
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        self.assertEqual(holophyte.board.failure_history(conn, 1), [])

    def test_a_passing_evidence_check_parks_without_a_strike(self):
        fake, _ = self.loop(Declare("test the modal"), REPRODUCED)

        self.assertEqual(fake.roles, ["implement", "adjudicate"])
        self.assertIn(DECLARED, fake.turns[0].goal)
        self.assertEqual(self.read("SELECT round, verdict FROM reviewRounds"),
                         [(1, "pass")])
        self.assert_parked_on_maintainer()

    def test_a_failed_check_is_round_one_and_a_redeclared_fix_is_checked_again(self):
        fake, _ = self.loop(Declare("test the modal"), REFUSED,
                            Declare("open the modal in the test"), REPRODUCED)

        self.assertEqual(fake.roles,
                         ["implement", "adjudicate", "implement", "adjudicate"])
        ((rnd, verdict, findings),) = self.read(
            "SELECT round, verdict, findings FROM reviewRounds WHERE round = 1")
        self.assertEqual((rnd, verdict), (1, "changes_requested"))
        self.assertEqual([f["message"] for f in json.loads(findings)], [REASON])
        self.assertIn(REASON, fake.turns[2].goal)
        self.assert_parked_on_maintainer()

    def test_a_fix_without_the_declaration_goes_to_an_ordinary_review(self):
        fake, _ = self.loop(Declare("test the modal"), REFUSED,
                            Commit("fix the modal"), APPROVE)

        self.assertEqual(fake.roles,
                         ["implement", "adjudicate", "implement", "review"])
        self.assertIn(REASON, fake.turns[2].goal)
        self.assertIn("READ-ONLY code reviewer", fake.turns[3].goal)
        self.assertEqual(self.read("SELECT round, verdict FROM reviewRounds"),
                         [(1, "changes_requested"), (2, "pass")])
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM runEvents WHERE kind = 'not_reproduced'"),
            [(0,)])

    def test_a_one_round_cap_still_gets_the_ordinary_round_two(self):
        self.configure("[loop]\nreview_rounds = 1\nreview_rounds_max = 1\n")
        fake, _ = self.loop(Declare("test the modal"), REFUSED,
                            Commit("fix the modal"), REQUEST_CHANGES,
                            Commit("fix again"), Reply("Mergeable.\nVERDICT: PASS"))

        self.assertEqual(fake.roles, ["implement", "adjudicate", "implement",
                                      "review", "implement", "adjudicate"])
        self.assertIn("READ-ONLY code reviewer", fake.turns[3].goal)
        self.assertEqual(self.read("SELECT round, verdict FROM reviewRounds"),
                         [(1, "changes_requested"), (2, "changes_requested"),
                          (3, "pass")])

    def test_a_declaration_whose_verify_fails_is_set_aside(self):
        provider = StubProvider(dict(a_task(), verify="false"))
        fake, _ = self.loop(Declare("test the modal"), REQUEST_CHANGES, Idle(),
                            provider=provider)

        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        self.assertIn("READ-ONLY code reviewer", fake.turns[1].goal)
        ((summary,),) = self.read("SELECT summary FROM runEvents"
                                  " WHERE kind = 'not_reproduced_set_aside'")
        self.assertIn("set aside: verify failed", summary)
        self.assertEqual(self.read("SELECT round, verdict FROM reviewRounds"),
                         [(1, "changes_requested")])


class ResumedRouteTests(LoopFixture):
    """A pause on the not-reproduced route resumes on it, not in an
    ordinary review."""

    def resume(self, *script):
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("paused",)])
        command(self.project, "KO-131", None, resume=True)
        fake, _ = self.loop(*script)
        self.assertEqual(self.read(
            "SELECT outcome, parkKind FROM runs ORDER BY id"),
            [("paused", None), (None, "not_reproduced")])
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        self.assertEqual(holophyte.board.failure_history(conn, 1), [])
        return fake

    def test_a_pause_after_the_declaring_turn_resumes_at_the_evidence_check(self):
        self.loop(DeclareThenPause(self.db))

        fake = self.resume(REPRODUCED)

        self.assertEqual(fake.roles, ["adjudicate"])

    def test_a_pause_during_the_check_resumes_the_fix_that_may_redeclare(self):
        self.loop(Declare("test the modal"), RefuseThenPause(self.db))

        fake = self.resume(Declare("open the modal in the test"), REPRODUCED)

        self.assertEqual(fake.roles, ["implement", "adjudicate"])
        self.assertIn(REASON, fake.turns[0].goal)


class AnsweredParkTests(LoopFixture):
    """The park's `--requeue` and `--approve` answers, through the factory's
    own commands (KO-658)."""

    def test_requeue_after_added_detail_is_a_fresh_attempt_without_a_strike(self):
        self.loop(Declare("test the modal"), REPRODUCED)

        holophyte.operator.requeue(self.project, "KO-131",
                                   "the name empties after a second rename",
                                   out=io.StringIO())

        self.assertEqual(self.read(
            "SELECT runId, action FROM interventions"
            " WHERE action != 'migrate'"), [(1, "requeue")])
        ((summary,),) = self.read("SELECT summary FROM runEvents"
                                  " WHERE runId = 1 AND kind = 'intervention'")
        self.assertIn("the name empties after a second rename", summary)
        self.assertEqual(self.read("SELECT outcome FROM runs"),
                         [("abandoned",)])
        self.assertEqual(self.read("SELECT status, blockedQuestion FROM tickets"),
                         [("ready", None)])

        fake, _ = self.loop(Commit("fix the modal"), APPROVE)

        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertIn("fix the modal", self.subjects())
        self.assertEqual(self.read("SELECT status FROM tickets"), [("merged",)])
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("abandoned",), ("merged",)])
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        self.assertEqual(holophyte.board.failure_history(conn, 1), [])

    def test_approve_lands_the_tests_locally_with_no_agent_turn(self):
        self.configure('[merge]\nmode = "local"\n')
        self.loop(Declare("test the modal"), REPRODUCED)
        test_commit = self.git("rev-parse", BRANCH).strip()
        holophyte.operator.approve(self.project, "KO-131", "keep the guard",
                                   out=io.StringIO())

        fake, _ = self.loop()

        self.assertEqual(fake.roles, [])
        self.assertEqual(self.git("merge-base", "--is-ancestor", test_commit,
                                  "main"), "")
        self.assertEqual(self.read("SELECT status FROM tickets"), [("merged",)])


class AnsweredParkPullRequestTests(MergeModeFixture):

    def test_an_approved_park_opens_a_pull_request_that_says_tests_only(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route()
        self.loop(Declare("test the modal"), REPRODUCED,
                  provider=self.provider())
        self.assertEqual(self.recorded(), [])
        holophyte.operator.approve(self.project, "KO-131", "keep the guard",
                                   out=io.StringIO())

        fake, _ = self.loop(
            Idle("TITLE: Fix the rename-guest modal\nThe modal keeps the name."),
            provider=self.provider())

        # The one turn is the pull request writer's, on the implement seat.
        (turn,) = fake.turns
        self.assertTrue(turn.goal.startswith("Write the pull request title"))
        first = self.pr_body.read_text().splitlines()[0]
        self.assertEqual(
            first, f"Tests only: the reported behaviour did not reproduce on"
            f" {self.base}; these tests are kept as a regression guard.")


class StorelessTests(LoopFixture):

    def test_a_storeless_run_parks_by_raising_merge_parked(self):
        # The fixture stands in for the supervisor and creates the store;
        # this run has none.
        self.db.unlink()
        fake =FakeAgent(Declare("test the modal"), REPRODUCED)
        with no_agent_processes(), \
                patch.object(holophyte.loop, "agent", fake), \
                self.assertRaises(MergeParked) as parked:
            holophyte.loop.run_task(self.project, a_task(),
                                    provider=StubProvider(a_task()))

        head = self.git("rev-parse", BRANCH).strip()
        self.assertIn(head[:12], str(parked.exception))
        self.assertEqual(fake.roles, ["implement", "adjudicate"])
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
