"""Wiring contract: the Linear mirror push from store status (state-model §1).

Holophyte owns ticket status; Linear is the notice board it is projected onto.
The loop moves its own mirror through §3's statuses and pushes each move to a
Linear workflow state through one function, so there is no second writer of
that state and nothing is ever read back. These tests drive `main()` with a
stub provider that records the pushes it is given — the oracle is the recorded
sequence and the stored status, not the factory's view of either.

Run: python3 -m unittest discover -s tests -p 'test_wiring*' -v
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


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
SPEC = importlib.util.spec_from_file_location("holophyte_factory", ROOT / "factory.py")
factory = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(factory)

import store  # noqa: E402 - after the sys.path insert above


ISSUE_UUID = "b0c1d2e3-4567-4890-abcd-ef0123456789"  # Linear's canonical id


class StubProvider:
    """The provider seam `main()` drives, recording every state it is pushed.

    `fail` makes each push raise, standing in for a Linear that is down or a
    token that has expired: the pushes are still recorded, so a test can tell
    "never attempted" from "attempted and refused".
    """

    TEAM = "team-under-test"

    def __init__(self, *tasks, fail=False):
        self.queue = list(tasks)
        self.fail = fail
        self.states = []
        self.comments = []

    def claim_next(self):
        return self.queue.pop(0) if self.queue else None

    def set_state(self, issue_id, state):
        self.states.append((issue_id, state))
        if self.fail:
            raise RuntimeError("linear is down")

    def comment(self, task_id, body):
        self.comments.append((task_id, body))


def a_task():
    """One ticket in the shape `linear_provider.parse_task()` returns.

    Specced: criteria and a verify command, so the store's §2 routing puts the
    mirror in `ready` and the claim's `ready -> in_flight` is a move §3 draws.
    """
    return {"id": "KO-132", "issue_id": ISSUE_UUID, "title": "push the mirror",
            "verify": "echo ok", "budget_min": 5, "contracts": [],
            "criteria": ["Given a status, when it changes, then Linear is told"]}


class MirrorPushTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.target = root / "repo"
        self.target.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.target, check=True)
        # Close-out commits FINDINGS.md in the target, so the fixture needs a
        # git identity of its own rather than the developer's global one.
        for key, value in (("user.email", "factory@example.invalid"),
                           ("user.name", "Factory Test")):
            subprocess.run(["git", "config", key, value],
                           cwd=self.target, check=True)
        self.db = root / "repo.holophyte.db"
        for name, value in (("TARGET", self.target), ("STORE_PATH", self.db),
                            ("WORKTREES", root / "repo.worktrees")):
            patcher = patch.object(factory, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def read(self, sql):
        """Query the store over a connection the factory never touched."""
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn.execute(sql).fetchall()

    def status(self):
        (status,), = self.read("SELECT status FROM tickets")
        return status

    def warnings(self):
        return [summary for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'warning' ORDER BY seq")]

    def loop(self, merged=True, provider=None):
        """Run the loop over one task, with the run itself stubbed out."""
        provider = provider or StubProvider(a_task())
        with patch.object(factory, "run_task", return_value=merged):
            factory.main(provider)
        return provider

    def test_the_claim_pushes_in_progress_exactly_once(self):
        """§3's `ready -> in_flight`, as the board sees it. The push happens
        at the claim and only there: the provider no longer sets a state of
        its own when it hands the task over, so a run that has started is one
        In Progress on the board, not two."""
        seen = {}

        def spy(task, conn=None, run_id=None):
            seen["states"] = list(provider.states)
            seen["status"] = self.status()
            return True

        provider = StubProvider(a_task())
        with patch.object(factory, "run_task", spy):
            factory.main(provider)

        self.assertEqual(seen["states"], [(ISSUE_UUID, "In Progress")])
        self.assertEqual(seen["status"], "in_flight")

    def test_a_merged_run_pushes_done_and_no_other_state_call_is_made(self):
        """The whole Linear-state story of a merged run: In Progress at the
        claim, Done at the merge, nothing in between. The merge itself no
        longer completes the ticket directly, so this sequence is the entire
        projection."""
        provider = self.loop(merged=True)

        self.assertEqual(provider.states,
                         [(ISSUE_UUID, "In Progress"), (ISSUE_UUID, "Done")])
        self.assertEqual(self.status(), "merged")
        self.assertEqual(self.warnings(), [])

    def test_a_run_that_did_not_merge_leaves_the_ticket_in_flight(self):
        """A failed run preserves its branch for a human, so the ticket is
        still in flight and the board must go on saying so: there is no status
        change, and therefore nothing to push."""
        provider = self.loop(merged=False)

        self.assertEqual(provider.states, [(ISSUE_UUID, "In Progress")])
        self.assertEqual(self.status(), "in_flight")
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])

    def test_a_push_that_fails_warns_and_leaves_the_run_untouched(self):
        """The projection is best-effort and the store is the truth: a Linear
        that refuses every push leaves the run merged, the ticket merged, and
        one warning per attempt in the run's event stream — a stale board, not
        a broken loop."""
        provider = self.loop(merged=True, provider=StubProvider(a_task(),
                                                               fail=True))

        self.assertEqual(provider.states,
                         [(ISSUE_UUID, "In Progress"), (ISSUE_UUID, "Done")])
        self.assertEqual(self.status(), "merged")
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual(len(self.warnings()), 2)
        for state, warning in zip(("In Progress", "Done"), self.warnings()):
            self.assertIn("KO-132", warning)
            self.assertIn(state, warning)
            self.assertIn("linear is down", warning)


if __name__ == "__main__":
    unittest.main()
