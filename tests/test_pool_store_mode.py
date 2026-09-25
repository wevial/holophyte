"""Phase 3 stage 3: in store mode the scheduler counts the store's queue.

The board is listed at most once a `[loop] tick_sec` however many ticks
the pool takes, the pool is sized by `store.read.claimable()`, a board
whose listing raises still leaves the pool the queue the store holds,
and neither the board's claim nor the empty pass's `_park_unlisted()` is
ever asked. Real store and scheduler; the spawn and wait seams are the
fixture's `FakePool`.

Run: python3 -m unittest discover -s tests -p 'test_pool_store_mode.py' -v
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loop_fixture import TICK, VALID_BODY, FakePool, LoopFixture  # noqa: E402

import holophyte.board  # noqa: E402
import holophyte.operator  # noqa: E402
import holophyte.pool  # noqa: E402
import store  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.runs import open_store  # noqa: E402
from provider import FileProvider  # noqa: E402

TICK_SEC = 60
CONFIG = ('[board]\nmode = "store"\nproject_id = "project-1"\n'
          f'team = "team-1"\n\n[loop]\nworkers = 2\ntick_sec = {TICK_SEC}\n')


class CountingFiles(FileProvider):
    """A store-mode file board counting its listings, whose own claim is
    never the scheduler's to ask; `broken` makes the listing raise."""

    store_mode = True

    def __init__(self, root):
        super().__init__(root)
        self.listed = 0
        self.broken = False

    def listing(self):
        self.listed += 1
        if self.broken:
            raise RuntimeError("the board is down")
        return super().listing()

    def claim_next(self, skip=(), order="identifier"):
        raise AssertionError("a store-mode scheduler asked the board to claim")


def never(*args, **kwargs):
    raise AssertionError("a store-mode pass parked its unlisted rows")


class StoreModePoolTests(LoopFixture):
    def setUp(self):
        super().setUp()
        self.configure(CONFIG)
        files = self.target.parent / "team-1"
        files.mkdir()
        for n in (1, 2, 3):
            (files / f"KO-{n}.md").write_text(VALID_BODY)
        self.board = CountingFiles(files)
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, self.board.team, self.target)

    def run_scheduler(self, exits):
        pool = FakePool(exits)
        out = io.StringIO()
        with patch.object(holophyte.pool, "SPAWN", pool.spawn), \
                patch.object(holophyte.pool, "WAIT", pool.wait), \
                patch("holophyte.claim._park_unlisted", never), \
                patch.object(sys, "orig_argv",
                             ["python3", "-u", "factory.py", str(self.target)]), \
                patch.object(sys, "stdout", out):
            self.rc = holophyte.operator.main(self.project, self.board)
        self.out = out.getvalue()
        return pool

    def ticket(self, identifier):
        return self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?",
            (identifier,)).fetchone()[0]

    def merge(self, *identifiers):
        for identifier in identifiers:
            store.tickets.walk_ticket(self.conn, self.ticket(identifier),
                                      "merged")

    def test_the_pool_is_sized_by_the_store_queue_and_lists_once(self):
        pool = self.run_scheduler([
            (holophyte.pool.WORKER_MERGED,
             lambda: self.merge("KO-1", "KO-2", "KO-3")),
            (holophyte.pool.WORKER_MERGED, None)])

        self.assertEqual(len(pool.spawned), 2)
        self.assertEqual(self.board.listed, 1)
        self.assertEqual(self.rc, 0)
        self.assertIn("board mode store: claims come from the store's queue",
                      self.out)

    def test_a_tick_inside_tick_sec_does_not_list_and_one_after_does(self):
        for n in (2, 3):
            (self.board.root / f"KO-{n}.md").unlink()
        listed = []

        def lease():
            listed.append(self.board.listed)
            store.claim(self.conn, self.project_id, self.ticket("KO-1"))

        def age_the_ask():
            listed.append(self.board.listed)
            with self.conn:
                self.conn.execute(
                    "UPDATE projects SET boardAskedAt = boardAskedAt - ?",
                    (TICK_SEC * 1000 + 1,))

        def merged():
            listed.append(self.board.listed)
            self.merge("KO-1")

        pool = self.run_scheduler([(TICK, lease), (TICK, age_the_ask),
                                   (holophyte.pool.WORKER_MERGED, merged)])

        self.assertEqual(listed, [1, 1, 2])
        self.assertEqual(len(pool.spawned), 1)
        self.assertEqual(pool.timeouts, [TICK_SEC] * 3)

    def test_a_board_that_cannot_list_still_leaves_the_store_queue(self):
        for n in (1, 2, 3):
            holophyte.board.mirror_task(self.conn, self.project_id,
                                        self.board.fetch_task(f"KO-{n}"))
        self.board.broken = True

        pool = self.run_scheduler([
            (holophyte.pool.WORKER_MERGED,
             lambda: self.merge("KO-1", "KO-2", "KO-3")),
            (holophyte.pool.WORKER_MERGED, None)])

        self.assertEqual(len(pool.spawned), 2)
        self.assertEqual(self.board.listed, 1)
        self.assertIn("queue mirror skipped", self.out)

