"""Factory loop control flow, driven end to end with zero agent calls.

The paths under test are the ones that used to need live agents to exercise:
a clean approval that merges, both review rounds spending their findings and
their fix rounds before a terminal PASS, the two ways adjudication refuses to
merge, and the failure-pattern escalation that stops a ticket the loop keeps
failing on from being claimed again. `tests/fake_agent.py` scripts the agent
turns; everything else is real — a real throwaway repo, real worktrees, the
real verify gate, the real `--no-ff` merge — so what these tests assert is the
loop's behavior and not a model of it.

Run: python3 -m unittest discover -s tests -p 'test_factory_loop*' -v
"""
from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
# `fake_agent` is a helper, not a test module: discovery never imports it, and
# how this file is imported decides whether `tests/` is on the path at all.
# Putting it there explicitly makes `discover -s tests` and `-m unittest
# tests.test_factory_loop` resolve the harness the same way.
sys.path.insert(0, str(HERE))
SPEC = importlib.util.spec_from_file_location("holophyte_factory", ROOT / "factory.py")
factory = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(factory)

from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    FAIL,
    MALFORMED,
    PASS,
    REQUEST_CHANGES,
    Commit,
    FakeAgent,
    no_agent_processes,
)

# The branch the loop cuts for the task below. Spelled out rather than derived
# from `factory`'s slug rule: an expectation computed by the code under test
# is not an expectation.
BRANCH = "task/add-a-thing"


class StubProvider:
    """The provider seam `main()` drives, queueing tasks it hands out in order."""

    TEAM = "team-under-test"

    def __init__(self, *tasks):
        self.queue = list(tasks)
        self.states = []
        self.comments = []

    def claim_next(self):
        return self.queue.pop(0) if self.queue else None

    def set_state(self, issue_id, state):
        self.states.append((issue_id, state))

    def comment(self, task_id, body):
        self.comments.append((task_id, body))


def a_task(n=1):
    """One ticket in the shape `linear_provider.parse_task()` returns."""
    return {"id": f"KO-13{n}", "issue_id": f"iss-13{n}", "title": "add a thing",
            "verify": "echo ok", "budget_min": 5, "contracts": [],
            "criteria": ["Given the thing, when it runs, then it works"]}


class LoopTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.target = root / "repo"
        self.worktrees = root / "repo.worktrees"
        self.target.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "factory@example.invalid")
        self.git("config", "user.name", "Factory Test")
        (self.target / "README.md").write_text("base\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "main").strip()

        self.db = root / "repo.holophyte.db"
        for name, value in (("TARGET", self.target), ("STORE_PATH", self.db),
                            ("WORKTREES", self.worktrees)):
            patcher = patch.object(factory, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.target),
                              check=True, capture_output=True, text=True).stdout

    def loop(self, *script, provider=None):
        """Run `main()` over the queued tasks with the script answering agents.

        Returns the fake and the spawn guard, so a test can read both the
        turns the loop took and the processes it did not start.
        """
        fake = FakeAgent(*script)
        provider = provider or StubProvider(a_task())
        with no_agent_processes() as guard:
            with patch.dict(sys.modules, {"linear_provider": provider}):
                with patch.object(factory, "agent", fake):
                    factory.main(provider)
        return fake, guard

    def read(self, sql):
        """Query the store over a connection the factory never touched."""
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn.execute(sql).fetchall()

    def subjects(self, rev="main"):
        return self.git("log", rev, "--format=%s").splitlines()

    def transitions(self):
        """The edges the run's narrative stream says it walked."""
        return [summary.split(":")[0] for (summary,) in
                self.read("SELECT summary FROM runEvents ORDER BY seq")]

    def branches(self):
        return [line[2:].strip() for line in
                self.git("branch", "--list").splitlines()]

    # --- the clean run ---------------------------------------------------

    def test_a_script_ending_in_approve_merges_without_spawning_an_agent(self):
        """One implement turn, one APPROVE: the branch reaches main, the run
        row says merged, and nothing anywhere under the loop started a real
        agent process."""
        fake, guard = self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(guard.spawned, [])
        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertIn("the scripted work", self.subjects())
        self.assertNotIn(BRANCH, self.branches())  # merged, so deleted
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    # --- both rounds spent, then a terminal PASS -------------------------

    def test_two_findings_rounds_then_adjudication_pass_merges_the_fixes(self):
        """REQUEST_CHANGES twice spends both review rounds and both fix
        rounds, so the loop falls through to the terminal adjudication; a PASS
        there merges, and every fix commit is in main's history."""
        fake, _ = self.loop(Commit("first cut"), REQUEST_CHANGES,
                            Commit("fix round 1"), REQUEST_CHANGES,
                            Commit("fix round 2"), PASS)

        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "review", "implement", "adjudicate"])
        self.assertEqual(self.transitions(),
                         ["claimed -> working",
                          "working -> verifying",
                          "verifying -> reviewing",
                          "reviewing -> addressing",
                          "addressing -> verifying",
                          "verifying -> reviewing",
                          "reviewing -> addressing",
                          "addressing -> verifying",
                          "verifying -> reviewing",
                          "reviewing -> merge_gate",
                          "merge_gate -> merging",
                          "merging -> done"])
        subjects = self.subjects()
        self.assertIn("fix round 1", subjects)
        self.assertIn("fix round 2", subjects)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    # --- adjudication refuses --------------------------------------------

    def test_adjudication_fail_preserves_the_branch_and_stops_the_loop(self):
        """FAIL is terminal and has no fix round: main is untouched, the
        branch and its worktree are left where a human can pick them up, and
        the next queued ticket is never claimed."""
        provider = StubProvider(a_task(1), a_task(2))
        self.loop(Commit("first cut"), REQUEST_CHANGES,
                  Commit("fix round 1"), REQUEST_CHANGES,
                  Commit("fix round 2"), FAIL, provider=provider)

        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertIn(BRANCH, self.branches())
        self.assertIn("fix round 2", self.subjects(BRANCH))
        self.assertTrue((self.worktrees / "add-a-thing").exists())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(len(provider.queue), 1)  # the loop stopped

    def test_an_adjudication_reply_with_no_verdict_is_read_as_fail(self):
        """A reply that names no verdict is not an approval by omission: the
        run fails like an explicit FAIL and the unreadable reply is kept as
        the round's finding."""
        self.loop(Commit("first cut"), REQUEST_CHANGES,
                  Commit("fix round 1"), REQUEST_CHANGES,
                  Commit("fix round 2"), MALFORMED)

        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertIn(BRANCH, self.branches())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(
            self.read("SELECT round, verdict FROM reviewRounds ORDER BY round"),
            [(1, "changes_requested"), (2, "changes_requested"), (3, "error")])

    # --- failure-pattern escalation --------------------------------------

    def fail_once(self):
        """Drive one whole run of the ticket that ends in a terminal FAIL."""
        self.loop(Commit("first cut"), REQUEST_CHANGES, Commit("fix round 1"),
                  REQUEST_CHANGES, Commit("fix round 2"), FAIL)

    def offer_again(self):
        """Offer the same ticket back, as a stale board does. No agent turns.

        The loop never reaches an agent on this pass, so the empty script is
        not laziness: a turn asked for here would be the loop re-implementing
        a ticket it had already failed on, and `FakeAgent` fails the test
        rather than answering one.
        """
        provider = StubProvider(a_task())
        self.loop(provider=provider)
        return provider

    def status(self):
        (status,), = self.read("SELECT status FROM tickets")
        return status

    def attempts(self):
        return self.read("SELECT attempt FROM runs ORDER BY id")

    def test_one_failed_run_leaves_the_ticket_claimable(self):
        """One failure is not a pattern: the ticket is left in flight rather
        than parked, and when the board offers it back the claim path lets it
        through — a second run row is the lease being taken for a second
        attempt, whatever that attempt then runs into."""
        self.fail_once()

        self.assertEqual(self.status(), "in_flight")

        self.offer_again()

        self.assertEqual(self.attempts(), [(1,), (2,)])

    def test_the_second_failure_blocks_the_ticket_and_reports_both_runs(self):
        """At the threshold the ticket stops being open work: the store parks
        it for an operator, the board is told, and one comment accounts for
        every failed run by the reason that run actually ended on."""
        self.fail_once()

        provider = self.offer_again()

        self.assertEqual(self.status(), "blocked_on_operator")
        self.assertEqual(provider.states, [("iss-131", "Todo")])
        (issue_id, body), = provider.comments
        self.assertEqual(issue_id, "iss-131")
        reasons = self.read("SELECT attempt, outcomeReason FROM runs"
                            " WHERE outcome = 'failed' ORDER BY attempt")
        self.assertEqual(len(reasons), 2)
        for attempt, reason in reasons:
            self.assertIn(f"attempt {attempt}: {reason}", body)

    def test_a_blocked_ticket_is_not_claimed_again(self):
        """The board says Todo — that is where `blocked_on_operator` projects
        — and offers the ticket back anyway. The claim path refuses it on the
        store's count instead: no run is opened, so no worktree is cut and no
        agent is paid to fail a third time."""
        self.fail_once()
        self.offer_again()
        blocked_at = self.attempts()

        provider = self.offer_again()

        self.assertEqual(self.attempts(), blocked_at)  # nothing was claimed
        self.assertEqual(provider.states, [])
        self.assertEqual(provider.comments, [])
        self.assertEqual(self.status(), "blocked_on_operator")

    def test_a_blocked_ticket_does_not_block_a_different_one(self):
        """The count is per ticket: the next ticket is worked and merged."""
        self.fail_once()
        self.offer_again()
        other = dict(a_task(2), title="add another thing")

        self.loop(Commit("the other work"), APPROVE,
                  provider=StubProvider(other))

        self.assertIn("the other work", self.subjects())
        self.assertEqual(
            self.read("SELECT linearIdentifier, status FROM tickets"
                      " ORDER BY id"),
            [("KO-131", "blocked_on_operator"), ("KO-132", "merged")])


if __name__ == "__main__":
    unittest.main()
