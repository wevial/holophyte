"""A loop's witness child waits for main to be quiet before it starts."""
from __future__ import annotations

import io
import os
import sys
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from loop_fixture import VALID_BODY, LoopFixture  # noqa: E402

import holophyte.cli.operator  # noqa: E402
import holophyte.loop.pool  # noqa: E402
import linear_provider  # noqa: E402
import store  # noqa: E402
import store.board  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from provider import board_for  # noqa: E402
from store.stories import approve_story, file_story  # noqa: E402
from tests.test_native_loop import NATIVE, no_linear  # noqa: E402
from tests.test_witness_runner import PASSES, PYTHON  # noqa: E402


class _Ticked(BaseException):
    """The scheduler reached its first wait: the end of its first tick."""


class QuietWindowTests(LoopFixture):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.configure(NATIVE + "[loop]\nworkers = 2\n")
        self.board = board_for(self.project)
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, self.board.team, self.project.path)

    def approved_story(self):
        parent, child = (
            self.conn.execute(
                "SELECT id FROM tickets WHERE linearIdentifier = ?",
                (store.board.file_ticket(self.conn, self.project_id, "NAT",
                                         VALID_BODY, column="backlog"),),
            ).fetchone()[0]
            for _ in range(2))
        file_story(self.conn, parent, [{
            "key": "W1", "criterion": "W1 holds", "file": "tests/test_w1.py",
            "command": f"{PYTHON} -m unittest tests.test_w1",
            "source": PASSES}], [(child, "completes", ["W1"])])
        (revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?", (parent,)).fetchone()
        approve_story(self.conn, parent, revision, "operator", "go")

    def first_tick(self):
        """The command lines the scheduler spawned before its first wait."""
        spawned = []

        def spawn(argv, **kwargs):
            spawned.append(argv)
            return type("Child", (), {"pid": 5001})()

        def stop(*args, **kwargs):
            raise _Ticked

        with patch.object(holophyte.loop.pool, "SPAWN", spawn), \
                patch.object(holophyte.loop.pool, "WAIT", stop), \
                patch.object(holophyte.loop.pool, "sleep", stop), \
                patch.object(sys, "orig_argv",
                             ["python3", "factory.py", str(self.target)]), \
                patch.object(sys, "stdout", io.StringIO()):
            try:
                holophyte.cli.operator.main(self.project, self.board)
            except _Ticked:
                return [argv[-1] for argv in spawned]
        self.fail(f"the scheduler returned on its first tick after {spawned}")

    def test_a_tip_younger_than_the_default_window_starts_no_witness_child(self):
        self.approved_story()
        age = time.time() - int(self.git("log", "-1", "--format=%ct", "main"))
        self.assertLess(age, 300)

        self.assertEqual(self.first_tick(), [])
