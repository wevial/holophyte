"""A hold arriving during scheduling drains workers without admitting more."""
from unittest.mock import patch

import holophyte.pool
import holophyte.runs
import store
from tests import test_pool


class PoolHoldTests(test_pool.LoopFixture):
    run_scheduler = test_pool.PoolTests.run_scheduler

    def test_disabled_loop_does_not_claim(self):
        provider = test_pool.StubProvider(test_pool.a_task(1))
        conn = holophyte.runs.open_store(self.tgt)
        self.addCleanup(conn.close)
        project = store.ensure_project(conn, provider.team, self.target)
        store.set_admission(conn, project, "disabled", "retired")
        with patch.object(provider, "ready_issues",
                          wraps=provider.ready_issues) as ready:
            pool = self.run_scheduler(1, provider, [])
        ready.assert_not_called()
        self.assertEqual(pool.spawned, [])
        self.assertIn("disabled: retired", self.out)
        self.assertEqual(conn.execute("SELECT count(*) FROM runs").fetchone(), (0,))

    def test_hold_drains_workers_and_acknowledges_return_with_exit_status(self):
        for exit_code, expected in ((holophyte.pool.WORKER_MERGED, 0),
                                    (holophyte.pool.WORKER_FAILED, 0),
                                    (-9, 1)):
            with self.subTest(exit_code=exit_code):
                provider = test_pool.StubProvider(
                    test_pool.a_task(1), test_pool.a_task(2))
                conn = holophyte.runs.open_store(self.tgt)
                try:
                    project = store.ensure_project(conn, provider.team, self.target)
                    restart = store.record_loop_restart(conn, project, "candidate")

                    def still_draining():
                        self.assertEqual(conn.execute(
                            "SELECT returnedAt FROM loopRestarts WHERE id = ?",
                            (restart,)).fetchone(), (None,))
                        self.assertEqual(listing.call_count, calls_at_hold[0])

                    def hold_while_running():
                        store.hold(conn, project, "reboot pending")
                        provider.queue.append(test_pool.a_task(3))
                        calls_at_hold.append(listing.call_count)

                    calls_at_hold = []
                    with patch.object(provider, "ready_issues",
                                      wraps=provider.ready_issues) as listing:
                        pool = self.run_scheduler(3, provider, [
                            (test_pool.TICK, hold_while_running),
                            (exit_code, still_draining),
                            (holophyte.pool.WORKER_MERGED, still_draining),
                        ])
                        self.assertEqual(listing.call_count, calls_at_hold[0])
                    self.assertEqual(len(pool.spawned), 2)
                    self.assertEqual(len(pool.reaped), 2)
                    self.assertEqual(pool.alive, [])
                    self.assertEqual(self.rc, expected)
                    self.assertIn(f"project {self.target} held: reboot pending",
                                  self.out)
                    self.assertIsNotNone(conn.execute(
                        "SELECT returnedAt FROM loopRestarts WHERE id = ?",
                        (restart,)).fetchone()[0])
                    store.release_hold(conn, project, "next scenario")
                finally:
                    conn.close()
