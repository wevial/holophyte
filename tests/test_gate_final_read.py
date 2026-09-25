"""In store mode the merge gate reads the board once more (KO-746): a
ticket canceled on the board ends its run `abandoned` with the branch
kept, and a ticket edited while the run worked is requeued under a
`requeue` intervention rather than failed, so it costs no strike."""
import io
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import APPROVE, Commit  # noqa: E402
from loop_fixture import BRANCH, LoopFixture, StubProvider, a_task  # noqa: E402

import store.read  # noqa: E402
from holophyte.board import failure_history  # noqa: E402

SECOND = "Given the thing, when it runs twice, then it works twice"


class StoreBoard(StubProvider):
    """A store-mode board: `answer` is what `states()` gives for every
    identifier asked, or an exception it raises."""

    store_mode = True

    def __init__(self, task, answer):
        super().__init__(task)
        self.answer, self.asked_states = answer, []
        self.pushed = self.states
        self.states = self.board_states

    def board_states(self, identifiers):
        self.asked_states.append(list(identifiers))
        if isinstance(self.answer, Exception):
            raise self.answer
        return {i: dict(self.answer) for i in identifiers}

    def set_state(self, issue_id, state):
        self.pushed.append((issue_id, state))

    def listing(self):
        return [dict(task, blocked_by=[]) for task in self.queue]


OPEN = {"state": "open", "name": "In Progress", "column": "in_progress"}
CANCELED = {"state": "canceled", "name": "Canceled", "column": "canceled"}


class GateFinalReadTests(LoopFixture):
    def run_loop(self, provider):
        with patch.object(sys, "stdout", io.StringIO()):
            self.loop(Commit("the scripted work"), APPROVE, provider=provider)

    def test_a_drifted_ticket_is_requeued_without_a_strike(self):
        provider = StoreBoard(a_task(), OPEN)
        criteria = [*a_task()["criteria"], SECOND]
        provider.live["iss-131"] = dict(
            a_task(), criteria=criteria,
            body="".join(f"- [ ] {c}\n" for c in criteria))

        self.run_loop(provider)

        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("abandoned",)])
        self.assertEqual(self.read("SELECT status FROM tickets"), [("ready",)])
        ((source, note),) = self.read(
            "SELECT i.source, e.summary FROM interventions i"
            " JOIN runEvents e ON e.runId = i.runId AND e.kind = 'intervention'"
            " WHERE i.action = 'requeue'")
        self.assertEqual(source, "factory")
        self.assertIn("acceptanceCriteria", note)
        self.assertIn("the scripted work", self.subjects(BRANCH))
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        ((ticket,),) = self.read("SELECT id FROM tickets")
        self.assertEqual(failure_history(conn, ticket), [])
        self.assertIn(SECOND,
                      store.read.ticket_revisions(conn, ticket)[0].body)

    def test_a_ticket_canceled_at_the_gate_ends_abandoned_with_its_branch(self):
        provider = StoreBoard(a_task(), CANCELED)

        self.run_loop(provider)

        self.assertEqual(provider.asked_states, [["KO-131"]])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("abandoned",)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("abandoned",)])
        self.assertEqual(self.read(
            'SELECT source, "trigger" FROM interventions'
            " WHERE action = 'abort'"), [("factory", "linear_cancelled")])
        self.assertIn(BRANCH, self.branches())
        self.assertIn("the scripted work", self.subjects(BRANCH))

    def test_a_board_that_cannot_be_asked_still_merges(self):
        provider = StoreBoard(a_task(), RuntimeError("linear is down"))

        self.run_loop(provider)

        self.assertEqual(provider.asked_states, [["KO-131"]])
        self.assertIn("the scripted work", self.subjects())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
