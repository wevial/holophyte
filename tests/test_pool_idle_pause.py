"""An idle worker pauses the scheduler's spawning for one `[loop] tick_sec`.

Real store and scheduler; the spawn and wait seams are the fixture's
`FakePool` and the pause's clock is a fake the scripted exits advance.

Run: python3 -m unittest discover -s tests -p 'test_pool_idle_pause.py' -v
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loop_fixture import (  # noqa: E402
    TICK,
    FakePool,
    LoopFixture,
    StubProvider,
    a_task,
)

import holophyte.board.projection  # noqa: E402
import holophyte.cli.operator  # noqa: E402
import holophyte.loop.pool  # noqa: E402
import holophyte.loop.runs  # noqa: E402
import store  # noqa: E402
import store.tickets  # noqa: E402

TICK_SEC = 30
IDLE = holophyte.loop.pool.WORKER_IDLE
MERGED = holophyte.loop.pool.WORKER_MERGED


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class CountingPool(FakePool):
    """Records how many workers had been spawned at each wait."""

    def __init__(self, exits):
        super().__init__(exits)
        self.spawned_at_wait = []

    def wait(self, children, timeout):
        self.spawned_at_wait.append(len(self.spawned))
        return super().wait(children, timeout)


class IdlePauseTests(LoopFixture):
    def setUp(self):
        super().setUp()
        self.clock = Clock()

    def a_tick(self, then=None):
        def elapse():
            self.clock.now += TICK_SEC
            if then is not None:
                then()
        return TICK, elapse

    def run_scheduler(self, workers, provider, exits):
        self.configure(f"[loop]\nworkers = {workers}\n"
                       f"tick_sec = {TICK_SEC}\n")
        pool = CountingPool(exits)
        out = io.StringIO()
        with patch.object(holophyte.loop.pool, "SPAWN", pool.spawn), \
                patch.object(holophyte.loop.pool, "WAIT", pool.wait), \
                patch.object(holophyte.loop.pool, "monotonic", self.clock), \
                patch.object(sys, "orig_argv",
                             ["python3", "-u", "factory.py", str(self.target)]), \
                patch.object(sys, "stdout", out):
            self.rc = holophyte.cli.operator.main(self.project, provider)
        self.out = out.getvalue()
        self.assertEqual(pool.exits, [], "every scripted exit was waited for")
        return pool

    def test_a_tick_after_an_idle_exit_spawns_for_work_claimable_since(self):
        provider = StubProvider(a_task(1), a_task(2))
        conn = holophyte.loop.runs.open_store(self.project)
        self.addCleanup(conn.close)
        project_id = store.tickets.ensure_project(conn, provider.team,
                                                  self.target)

        def long_runner_holds_one_and_two_is_refused():
            store.claim(conn, project_id, holophyte.board.projection.mirror_task(
                conn, project_id, a_task(1)))
            conn.commit()
            provider.queue.remove(provider.queue[1])

        pool = self.run_scheduler(3, provider, [
            (IDLE, long_runner_holds_one_and_two_is_refused),
            self.a_tick(lambda: provider.queue.append(a_task(3))),
            (MERGED, provider.queue.clear),
            (MERGED, None),
        ])

        # Workers 1 and 2 at the start; worker 3 at the tick, while the
        # long-running worker 2 is still in flight.
        self.assertEqual(pool.spawned_at_wait, [2, 2, 3, 3])
        self.assertIn("[holo2] started worker 3 as pid 5003", self.out)
        self.assertEqual(pool.timeouts, [TICK_SEC] * 4)
        self.assertEqual(self.rc, 0)

    def test_repeated_idle_exits_spawn_at_most_one_worker_a_tick(self):
        provider = StubProvider(*(a_task(n) for n in range(1, 4)))

        pool = self.run_scheduler(4, provider, [
            (IDLE, None),
            self.a_tick(),
            (IDLE, None),
            self.a_tick(),
            (IDLE, provider.queue.clear),
            (IDLE, None),
            (IDLE, None),
        ])

        spawned = pool.spawned_at_wait + [len(pool.spawned)]
        after_each = [b - a for a, b in zip(spawned, spawned[1:])]
        # Three claimable tickets and free slots, yet only one probe a tick
        # and none on an idle exit.
        self.assertEqual(after_each, [0, 1, 0, 1, 0, 0, 0])
        self.assertEqual(pool.timeouts, [TICK_SEC] * 7)
        self.assertEqual(self.rc, 0)

    def test_any_exit_clears_the_pause_at_once(self):
        provider = StubProvider(*(a_task(n) for n in range(1, 4)))

        pool = self.run_scheduler(3, provider, [
            (IDLE, None),
            (MERGED, lambda: provider.queue.pop(0)),
            (MERGED, provider.queue.clear),
            (IDLE, None),
            (IDLE, None),
        ])

        # The merge refills both free slots with no time passed.
        self.assertEqual(self.clock.now, 1000.0)
        self.assertEqual(pool.spawned_at_wait, [3, 3, 5, 5, 5])
        self.assertEqual(self.rc, 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
