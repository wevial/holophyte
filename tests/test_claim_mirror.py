"""KO-425: a mirror row follows its ticket off the board's ready column.

The store mirrors a ticket when a loop pass sees it ready, and
`store.read.ready_tickets()` is what the supervisor counts as owed a
loop; nothing used to move the row when the ticket left the column
unclaimed -- pulled to Backlog, say, or blocked by a new relation. The
row stayed `ready`, the supervisor counted it every sweep, and each
relaunched pass found the board empty and exited. The empty pass now
reconciles: every `ready` row with no live run the board's ready listing
does not name is walked to `blocked_on_deps` and named in one line, so
the next sweep owes nothing; and a `blocked_on_deps` row whose ticket
the listing names again is walked back to `ready` when the pass mirrors
it, so a move back to Todo recovers the ticket with no operator step.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
# `fake_agent` is a helper, not a test module: discovery never imports it, and
# how this file is imported decides whether `tests/` is on the path at all.
# Putting it there explicitly makes `discover -s tests` and `-m unittest
# tests.<name>` resolve the harness the same way.
sys.path.insert(0, str(HERE))
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    FakePool,
    LoopFixture,
    StubProvider,
    a_task,
)

import holophyte.board  # noqa: E402 - after the sys.path insert above
import holophyte.operator  # noqa: E402
import holophyte.pool  # noqa: E402
import holophyte.runs  # noqa: E402
import store  # noqa: E402 - after the sys.path insert above
import store.tickets as tickets  # noqa: E402 - after the sys.path insert above


class OffBoardMirrorTests(LoopFixture):
    """The empty pass's mirror reconcile, both directions.

    A `ready` row whose ticket the board's ready listing no longer names
    waits on the board at `blocked_on_deps`; a `blocked_on_deps` row whose
    ticket the listing names again is `ready` once the pass has mirrored
    it. Both are the store's word following the board's, so the
    supervisor's `ready_tickets()` count stays honest.
    """

    def seed_ready(self, task, run=None):
        """`task` mirrored `ready` with no live run; with `run`, a failed
        run behind it so the ticket has a `lastRunId` the note lands on."""
        conn = store.open(str(self.db), migrate="owner")
        try:
            project = tickets.ensure_project(conn, StubProvider.TEAM,
                                             str(self.target))
            ticket = holophyte.board.mirror_task(conn, project, task)
            run_id = None
            if run:
                run_id = store.claim(conn, project, ticket)
                tickets.transition(conn, ticket, "in_flight")
                store.release(conn, run_id, "failed", run)
                store.requeue(conn, ticket, "ready to try again")
            conn.commit()
        finally:
            conn.close()
        return run_id

    def test_a_ready_row_whose_ticket_left_the_board_waits_on_it(self):
        """KO-134 was mirrored `ready` and ran once; the operator then
        pulled it to Backlog. The pass finds the board's ready column
        empty, walks the stale row to `blocked_on_deps` and names it in
        the one line, with the `note` on its last run -- the next sweep
        owes it nothing."""
        run_id = self.seed_ready(a_task(4), run="the run failed")

        out = self.main_output(provider=StubProvider())

        self.assertIn("[holo2] 1 mirror rows left the board's ready column;"
                      " waiting on the board: KO-134", out)
        self.assertIn("Linear has no ready tickets. done.", out)
        self.assertEqual(
            self.read("SELECT linearIdentifier, status, activeRunId"
                      " FROM tickets"),
            [("KO-134", "blocked_on_deps", None)])
        (note,) = self.read("SELECT runId, text FROM ledger"
                            " WHERE kind = 'note'")
        self.assertEqual(note[0], run_id)
        self.assertIn("KO-134", note[1])

    def test_a_ready_row_the_listing_still_names_is_left_alone(self):
        """The ticket is still in the board's ready column -- here held by
        another writer's lease label, which is what kept this pass from
        claiming it -- so the reconcile leaves its row `ready` for the
        claim that will take it."""
        self.configure('[report]\nhost_label = "writer-1"\n')
        held = dict(a_task(), labels=["holo:writer-2"])

        out = self.main_output(provider=StubProvider(held))

        self.assertIn("[holo2] KO-131 is leased by writer-2 on the board;"
                      " skipping it", out)
        self.assertNotIn("mirror rows left", out)
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("ready",)])
        self.assertEqual(self.read("SELECT id FROM runs"), [])

    def test_a_waiting_row_whose_ticket_is_listed_again_is_ready(self):
        """KO-131's mirror was walked to `blocked_on_deps` while the board
        held it off the ready column; it is back in Todo now, so the
        pass's mirror walks the row back to `ready`. The pass then
        refuses it on the lease another writer holds, and because the
        listing names it the reconcile does not park it again: the row
        ends `ready`, owed a loop."""
        self.configure('[report]\nhost_label = "writer-1"\n')
        held = dict(a_task(), labels=["holo:writer-2"])
        conn = store.open(str(self.db), migrate="owner")
        try:
            project = tickets.ensure_project(conn, StubProvider.TEAM,
                                             str(self.target))
            ticket = holophyte.board.mirror_task(conn, project, held)
            tickets.transition(conn, ticket, "blocked_on_deps")
            conn.commit()
        finally:
            conn.close()

        out = self.main_output(provider=StubProvider(held))

        self.assertIn("[holo2] KO-131 is leased by writer-2 on the board;"
                      " skipping it", out)
        self.assertNotIn("mirror rows left", out)
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("ready",)])
        self.assertEqual(self.read("SELECT id FROM runs"), [])

    def test_the_scheduler_recovers_a_relisted_ticket_before_the_count(self):
        """`workers = 2`: the scheduler mirrors the queue, then counts
        claimable tickets before spawning a worker. Recovery kept in
        `_admit_ticket()` ran only inside a worker -- and with the row
        still `blocked_on_deps` the count was zero, so no worker ever
        ran it and the relisted ticket stayed parked (the KO-425 review's
        reproduction)."""
        provider = StubProvider(a_task(1))
        conn = holophyte.runs.open_store(self.project)
        self.addCleanup(conn.close)
        project_id = tickets.ensure_project(conn, provider.team, self.target)
        ticket = holophyte.board.mirror_task(conn, project_id, a_task(1))
        tickets.transition(conn, ticket, "blocked_on_deps")
        conn.commit()

        def merged():
            # The spawned worker's merge: terminal status, off the board.
            tickets.walk_ticket(conn, ticket, "merged")
            conn.commit()
            provider.queue.clear()

        self.configure("[loop]\nworkers = 2\nstop_on_failure = false\n")
        pool = FakePool([(holophyte.pool.WORKER_MERGED, merged)])
        with patch.object(holophyte.pool, "SPAWN", pool.spawn), \
                patch.object(holophyte.pool, "WAIT", pool.wait), \
                patch.object(sys, "orig_argv",
                             ["python3", "-u", "factory.py",
                              str(self.target)]), \
                patch.object(sys, "stdout", io.StringIO()):
            self.rc = holophyte.operator.main(self.project, provider)

        # One worker for the relisted ticket; without the mirror-path
        # recovery the count was zero and nothing spawned.
        self.assertEqual(len(pool.spawned), 1)
        self.assertEqual(self.rc, 0)
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])
