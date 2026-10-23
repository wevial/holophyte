"""A scheduler pass that meets a locked store skips its tick instead of
exiting, keeps waiting on its live workers, and stops with status 1 after
`STORE_SKIPS` skipped ticks in a row; any other store error still raises.

Real store, real second `sqlite3` connection holding the write lock, real
scheduler; the spawn and wait seams are the fixture's `FakePool`.

Run: python3 -m unittest discover -s tests -p 'test_pool_locked_store.py' -v
"""
from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loop_fixture import TICK, VALID_BODY, FakePool, LoopFixture  # noqa: E402

import holophyte.admission  # noqa: E402
import holophyte.cli.operator  # noqa: E402
import holophyte.loop.pool  # noqa: E402
import store  # noqa: E402
import store.schema  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from provider import FileProvider  # noqa: E402

TICK_SEC = 60
CONFIG = ('[board]\nmode = "store"\nproject_id = "project-1"\n'
          f'team = "team-1"\n\n[loop]\nworkers = 2\ntick_sec = {TICK_SEC}\n')
SKIPPED = "scheduler tick skipped: store unavailable"


class StoreBoard(FileProvider):
    store_mode = True


class LockedStoreSchedulerTests(LoopFixture):
    def setUp(self):
        super().setUp()
        self.configure(CONFIG)
        files = self.target.parent / "team-1"
        files.mkdir()
        (files / "KO-1.md").write_text(VALID_BODY)
        self.board = StoreBoard(files)
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, self.board.team, self.target)
        self.holder = None

    def run_scheduler(self, exits):
        pool = FakePool(exits)
        out = io.StringIO()
        with patch.object(store.schema, "BUSY_TIMEOUT_S", 0.2), \
                patch.object(holophyte.loop.pool, "SPAWN", pool.spawn), \
                patch.object(holophyte.loop.pool, "WAIT", pool.wait), \
                patch.object(sys, "orig_argv",
                             ["python3", "-u", "factory.py", str(self.target)]), \
                patch.object(sys, "stdout", out):
            try:
                self.rc = holophyte.cli.operator.main(self.project, self.board)
            finally:
                self.out = out.getvalue()
        return pool

    def ticket(self):
        return self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = 'KO-1'"
        ).fetchone()[0]

    def asked_at(self):
        return self.conn.execute(
            "SELECT boardAskedAt FROM projects WHERE id = ?",
            (self.project_id,)).fetchone()[0]

    def lease_and_lock(self):
        """Lease the one ticket, make the next board ask due, then hold the
        store's write lock on a second connection."""
        store.claim(self.conn, self.project_id, self.ticket())
        with self.conn:
            self.conn.execute(
                "UPDATE projects SET boardAskedAt = boardAskedAt - ?",
                (TICK_SEC * 1000 + 1,))
        self.aged = self.asked_at()
        self.holder = sqlite3.connect(str(self.project.store_path))
        self.addCleanup(self.holder.close)
        self.holder.execute("BEGIN IMMEDIATE")

    def release(self):
        self.still_aged = self.asked_at()
        self.holder.execute("COMMIT")

    def merged(self):
        store.tickets.walk_ticket(self.conn, self.ticket(), "merged")

    def test_a_locked_pass_skips_its_tick_and_a_later_tick_writes(self):
        pool = self.run_scheduler([
            (TICK, self.lease_and_lock),
            (TICK, self.release),
            (holophyte.loop.pool.WORKER_MERGED, self.merged)])

        self.assertEqual(self.out.count(
            f"{SKIPPED} (database is locked); next tick in {TICK_SEC}s"), 1)
        self.assertEqual(self.still_aged, self.aged)
        self.assertGreater(self.asked_at(), self.aged)
        self.assertEqual(len(pool.spawned), 1)
        self.assertEqual(pool.reaped, [(5001, holophyte.loop.pool.WORKER_MERGED)])
        self.assertEqual(self.rc, 0)

    def test_a_lock_held_through_every_pass_reaps_then_stops_after_three_skips(self):
        pool = self.run_scheduler([
            (TICK, self.lease_and_lock), (TICK, None), (TICK, None),
            (holophyte.loop.pool.WORKER_FAILED, None)])

        self.assertEqual(self.out.count(SKIPPED), 3)
        lines = self.out.splitlines()
        self.assertTrue(lines[-1].endswith(
            "scheduler stopping: store unavailable for 3 consecutive ticks"
            " (database is locked)"), lines[-1])
        self.assertNotIn(SKIPPED, "\n".join(lines[-2:]))
        self.assertIn("worker 1 failed (exit 1)", lines[-2])
        self.assertEqual(pool.reaped, [(5001, holophyte.loop.pool.WORKER_FAILED)])
        self.assertEqual(self.rc, 1)

    def test_a_clean_tick_resets_the_count_and_other_errors_raise(self):
        real = holophyte.admission.reconcile_tick

        def scripted(*raised):
            calls = iter(raised)

            def reconcile_tick(*args):
                exc = next(calls, None)
                if exc is not None:
                    raise exc
                return real(*args)
            return reconcile_tick

        def locking():
            return sqlite3.OperationalError("locking protocol")

        def lease():
            store.claim(self.conn, self.project_id, self.ticket())

        with self.subTest("locking protocol, reset by a clean tick"):
            ticks = scripted(None, locking(), locking(), None,
                             locking(), locking())
            with patch.object(holophyte.admission, "reconcile_tick", ticks):
                pool = self.run_scheduler(
                    [(TICK, lease)] + [(TICK, None)] * 4
                    + [(holophyte.loop.pool.WORKER_MERGED, self.merged)])
            self.assertEqual(self.out.count(
                f"{SKIPPED} (locking protocol); next tick in {TICK_SEC}s"), 4)
            self.assertNotIn("scheduler stopping", self.out)
            self.assertEqual(pool.reaped,
                             [(5001, holophyte.loop.pool.WORKER_MERGED)])
            self.assertEqual(self.rc, 0)

        with self.subTest("another OperationalError propagates"):
            probe = sqlite3.OperationalError("no such table: probe")
            with patch.object(holophyte.admission, "reconcile_tick",
                              scripted(probe)), \
                    self.assertRaises(sqlite3.OperationalError) as caught:
                self.run_scheduler([])
            self.assertIs(caught.exception, probe)
            self.assertNotIn(SKIPPED, self.out)
