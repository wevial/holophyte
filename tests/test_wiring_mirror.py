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

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
import holophyte.loop  # noqa: E402 - after the sys.path insert above
import holophyte.target  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above

ISSUE_UUID = "b0c1d2e3-4567-4890-abcd-ef0123456789"  # Linear's canonical id


class StubProvider:
    """The provider seam `main()` drives, recording every state it is pushed.

    `fail` makes each push raise, standing in for a Linear that is down or a
    token that has expired: the pushes are still recorded, so a test can tell
    "never attempted" from "attempted and refused".
    """

    TEAM = "team-under-test"
    team = TEAM  # the `Provider` protocol's spelling

    def __init__(self, *tasks, fail=False):
        self.queue = list(tasks)
        self.fail = fail
        self.states = []
        self.comments = []

    def claim_next(self, skip=(), order="identifier"):
        """The first queued task the loop has not already refused.

        `skip` is honored rather than ignored because the real provider hands
        back the *same* head-of-queue ticket on every ask; a stub that popped
        blindly would let a loop that cannot skip look like one that can.
        """
        for i, task in enumerate(self.queue):
            if task["id"] not in skip:
                return self.queue.pop(i)
        return None

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
        # The `Target` the loop is handed, with the store and the worktrees
        # placed by hand: outside the target, never a file in it.
        self.tgt = holophyte.target.Target(
            path=self.target, holo_dir=root, store_path=self.db,
            config_path=root / "config.toml",
            worktrees=root / "repo.worktrees")

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
        with patch.object(holophyte.loop, "run_task", return_value=merged):
            holophyte.loop.main(self.tgt, provider)
        return provider

    def test_the_claim_pushes_in_progress_exactly_once(self):
        """§3's `ready -> in_flight`, as the board sees it. The push happens
        at the claim and only there: the provider no longer sets a state of
        its own when it hands the task over, so a run that has started is one
        In Progress on the board, not two."""
        seen = {}

        def spy(target, task, conn=None, run_id=None, provider=None):
            seen["states"] = list(provider.states)
            seen["status"] = self.status()
            return True

        provider = StubProvider(a_task())
        with patch.object(holophyte.loop, "run_task", spy):
            holophyte.loop.main(self.tgt, provider)

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

    def test_a_ticket_the_store_has_finished_is_not_worked_again(self):
        """§1 the other way round: the store decides what may be worked, so a
        board that is behind it cannot re-open finished work. A merged ticket
        whose Done push never landed is still non-terminal in Linear and gets
        offered again — `store.pickable()` refuses it before the claim, so no
        run is opened for it, the loop says why it moved on, and Done is
        pushed once more at the board that missed it: a skip that left the
        board alone would leave the ticket offered forever."""
        self.loop(merged=True, provider=StubProvider(a_task(), fail=True))
        self.assertEqual(self.status(), "merged")  # and the board never heard
        runs = self.read("SELECT id FROM runs")

        provider = StubProvider(a_task())
        with patch.object(holophyte.loop, "run_task") as run_task, \
                patch("builtins.print") as printed:
            holophyte.loop.main(self.tgt, provider)

        run_task.assert_not_called()
        self.assertEqual(self.read("SELECT id FROM runs"), runs)
        self.assertEqual(self.status(), "merged")
        self.assertEqual(provider.states, [(ISSUE_UUID, "Done")])
        notes = [c.args[0] for c in printed.call_args_list
                 if c.args and "skipping" in str(c.args[0])]
        self.assertEqual(len(notes), 1)
        self.assertIn("merged", notes[0])

    def test_the_run_of_a_refused_claim_does_not_keep_the_lease(self):
        """The second line of defense, for a ticket that stops being pickable
        between the pre-claim check and the claim: the refusal happens after
        the claim, so it owes the project its lease back like any other
        failure path — a stale ticket must not brick the queue for the
        tickets behind it. The race is staged by answering the pre-claim
        question with a yes the store would not give."""
        self.loop(merged=True, provider=StubProvider(a_task(), fail=True))

        with patch.object(holophyte.loop, "run_task"), \
                patch.object(store, "pickable",
                             return_value=store.Pickability(True, None)):
            holophyte.loop.main(self.tgt, StubProvider(a_task()))

        self.assertEqual(self.read("SELECT activeRunId FROM projects"),
                         [(None,)])
        outcomes = self.read("SELECT outcome, outcomeReason FROM runs"
                             " ORDER BY id")
        self.assertEqual(outcomes[-1][0], "failed")
        self.assertIn("no work started", outcomes[-1][1])


class SetStateTests(unittest.TestCase):
    """The push itself, at the provider seam `mirror_push()` calls.

    Linear answers a mutation it refused with `success: false` and no
    `errors`, so this is the only place that refusal can be noticed.
    """

    @classmethod
    def setUpClass(cls):
        # linear_provider refuses to import without a configured project.
        os.environ.setdefault("HOLO2_PROJECT_ID", "test-project")
        os.environ.setdefault("HOLO2_TEAM", "test-team")
        import linear_provider
        cls.provider = linear_provider

    def gql(self, success):
        """Stand in for Linear: the state lookup, then the update's verdict."""
        def fake(query, variables=None):
            if "workflowStates" in query:
                return {"workflowStates": {"nodes": [
                    {"id": "state-uuid", "name": "Done", "type": "completed"}]}}
            self.sent = variables
            return {"issueUpdate": {"success": success}}
        return patch.object(self.provider, "_gql", fake)

    def test_a_successful_update_pushes_the_resolved_state_id(self):
        with self.gql(True):
            self.provider.set_state(ISSUE_UUID, "Done")

        self.assertEqual(self.sent, {"id": ISSUE_UUID, "state": "state-uuid"})

    def test_an_update_linear_did_not_apply_raises(self):
        """`success: false` is a push that did not land, and a push that does
        not raise is one `mirror_push()` records as a projection that did."""
        with self.gql(False), self.assertRaises(RuntimeError) as caught:
            self.provider.set_state(ISSUE_UUID, "Done")

        self.assertIn(ISSUE_UUID, str(caught.exception))
        self.assertIn("Done", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
