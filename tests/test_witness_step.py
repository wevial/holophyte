"""The loop's witness step runs a pass for an open story whose ledger lacks
main's tip, in the serial loop once its queue is empty and in a child the
pool's scheduler spawns after its workers, under the project's witness
lock, and the host sweep starts a loop for such a story when none is live.
A held project gets neither.

Run: python3 -m unittest discover -s tests -p 'test_witness_step.py' -v
"""
from __future__ import annotations

import fcntl
import io
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import APPROVE, Commit, FakeAgent, no_agent_processes  # noqa: E402
from loop_fixture import ROOT, TICK, VALID_BODY, FakePool, LoopFixture  # noqa: E402

import holophyte.cli.operator  # noqa: E402
import holophyte.loop.adjudicate  # noqa: E402
import holophyte.loop.implement  # noqa: E402
import holophyte.loop.pool  # noqa: E402
import holophyte.loop.review_round  # noqa: E402
import store  # noqa: E402
import store.board  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.board import linear_provider  # noqa: E402
from holophyte.board.provider import board_for  # noqa: E402
from holophyte.config.config_tables import sweep_config  # noqa: E402
from holophyte.host.supervisor import (  # noqa: E402
    fresh_memory,
    reconcile_parked_pull_requests,
)
from holophyte.loop.runs import open_store  # noqa: E402
from holophyte.story.witness import witness_lock_path, witness_step  # noqa: E402
from holophyte.story.witness_running import mark_running  # noqa: E402
from store.stories import approve_story, file_story  # noqa: E402
from tests.test_native_loop import NATIVE, no_linear  # noqa: E402
from tests.test_witness_runner import PASSES, PYTHON  # noqa: E402

W1_FILE = "tests/test_w1.py"
PARKED = holophyte.loop.pool.WORKER_PARKED
QUIET = "[story]\nwitness_quiet_sec = 0\n"
LEDGER = ("SELECT witnessKey, mainSha, verdict, verifier FROM witnessResults"
          " ORDER BY id")


class WitnessStepTests(LoopFixture):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.use(NATIVE)

    def use(self, config):
        self.configure(config)
        self.board = board_for(self.project)
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, self.board.team, self.project.path)

    def approved_story(self, child_column,
                       command=f"{PYTHON} -m unittest tests.test_w1"):
        """An approved story whose one child, in `child_column`, completes
        W1 by landing `W1_FILE`, W1 running `command`; the parent's ticket
        id."""
        parent, child = (
            self.ticket_id(store.board.file_ticket(
                self.conn, self.project_id, "NAT", VALID_BODY, column=column))
            for column in ("backlog", child_column))
        file_story(self.conn, parent, [{
            "key": "W1", "criterion": "W1 holds", "file": W1_FILE,
            "command": command,
            "source": PASSES}], [(child, "completes", ["W1"])])
        (revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?", (parent,)).fetchone()
        approve_story(self.conn, parent, revision, "operator", "go")
        return parent

    def record(self):
        return Path(self.project.holo_dir) / "witness-running.json"

    def ticket_id(self, identifier):
        return self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?",
            (identifier,)).fetchone()[0]

    def step(self):
        out = io.StringIO()
        with patch.object(sys, "stdout", out):
            witness_step(self.project, self.conn, self.project_id)
        return out.getvalue()

    def file_ready(self):
        return self.ticket_id(store.board.file_ticket(
            self.conn, self.project_id, "NAT", VALID_BODY, column="ready"))

    def take(self, *ticket_ids):
        """What a worker's claim does to the queue: its ticket leaves it."""
        for ticket_id in ticket_ids:
            store.tickets.walk_ticket(self.conn, ticket_id, "in_flight")

    def schedule(self, exits):
        pool = FakePool(exits)
        with patch.object(holophyte.loop.pool, "SPAWN", pool.spawn), \
                patch.object(holophyte.loop.pool, "WAIT", pool.wait), \
                patch.object(sys, "orig_argv",
                             ["python3", "factory.py", str(self.target)]), \
                patch.object(sys, "stdout", io.StringIO()):
            holophyte.cli.operator.main(self.project, self.board)
        return pool

    @staticmethod
    def modes(pool):
        return [argv[-1] for argv in pool.spawned]

    def tip(self):
        return self.git("rev-parse", "main").strip()

    def sweep(self):
        """One host sweep pass with no loop live; the owed pairs of each
        loop start it made."""
        started = []
        with patch("holophyte.host.reconcile._reconcile_pull_requests"), \
                patch("holophyte.host.supervisor.linear_budget_low",
                      return_value=False), \
                patch("holophyte.host.supervisor.start_loop_for",
                      lambda target, conn, owed, *a, **k: started.append(owed)):
            reconcile_parked_pull_requests(
                self.project, self.conn, int(time.time() * 1000), self.board,
                io.StringIO(), knobs=sweep_config(self.project),
                memory=fresh_memory())
        return started

    def test_the_serial_loop_witnesses_the_childs_merge_before_it_exits(self):
        self.approved_story("ready")
        fake = FakeAgent(Commit("lands w1", path=W1_FILE, body=PASSES),
                         APPROVE)
        out = io.StringIO()
        with no_agent_processes(), patch.object(sys, "stdout", out), \
                patch.object(holophyte.loop.implement, "agent", fake), \
                patch.object(holophyte.loop.review_round, "agent", fake), \
                patch.object(holophyte.loop.adjudicate, "agent", fake), \
                patch("holophyte.review.freshness.critic_admits",
                      return_value=True):
            holophyte.cli.operator.main(self.project, self.board)

        self.assertIn("lands w1", self.subjects(), out.getvalue())
        self.assertNotEqual(self.tip(), self.base)
        self.assertEqual(self.read(LEDGER), [
            ("W1", self.tip(), "green", "loop")], out.getvalue())

    def test_the_pool_runs_a_pending_pass_before_it_returns(self):
        self.use(NATIVE + QUIET + "[loop]\nworkers = 2\n")
        self.approved_story("backlog")
        pool = self.schedule([(0, None)])

        self.assertEqual(self.modes(pool), ["--witness-step"])
        self.assertEqual(pool.reaped, [(pool.next_pid, 0)])
        self.assertEqual(pool.exits, [])
        self.assertEqual(pool.alive, [])

    def test_the_pool_spawns_its_workers_before_the_witness_child(self):
        self.use(NATIVE + QUIET + "[loop]\nworkers = 2\n")
        self.approved_story("backlog")
        first, second = self.file_ready(), self.file_ready()
        pool = self.schedule([(PARKED, lambda: self.take(first, second)),
                              (PARKED, None), (0, None)])

        self.assertEqual(self.modes(pool),
                         ["--worker", "--worker", "--witness-step"])
        self.assertEqual(self.read(LEDGER), [])

    def test_a_ticket_filed_during_a_pass_gets_a_worker_and_no_second_pass(self):
        self.use(NATIVE + QUIET + "[loop]\nworkers = 2\n")
        self.approved_story("backlog")
        first = self.file_ready()
        filed = []

        def park_and_file():
            self.take(first)
            filed.append(self.file_ready())

        pool = self.schedule([(PARKED, park_and_file),
                              (0, lambda: self.take(*filed)), (PARKED, None)])

        self.assertEqual(self.modes(pool),
                         ["--worker", "--witness-step", "--worker"])
        witness_pid = pool.next_pid - 1
        self.assertEqual([pid for pid, _ in pool.reaped],
                         [witness_pid - 1, witness_pid, witness_pid + 1])

    def test_a_pass_that_recorded_nothing_is_retried_a_tick_later(self):
        self.use(NATIVE + QUIET + "[loop]\nworkers = 2\ntick_sec = 60\n")
        self.approved_story("backlog")
        first = self.file_ready()
        filed, clock = [], [1000.0]

        def park_and_file():
            self.take(first)
            filed.append(self.file_ready())

        def a_tick_passes():
            clock[0] += 60

        with patch.object(holophyte.loop.pool, "monotonic", lambda: clock[0]):
            pool = self.schedule([
                (PARKED, park_and_file), (0, lambda: self.take(*filed)),
                (TICK, a_tick_passes),
                (PARKED, None), (0, self.step)])

        self.assertEqual(self.modes(pool), ["--worker", "--witness-step",
                                            "--worker", "--witness-step"])
        self.assertEqual(self.read(LEDGER), [("W1", self.base, "absent", "loop")])

    def test_a_tip_merged_during_a_pass_gets_its_own_pass_before_return(self):
        self.use(NATIVE + QUIET + "[loop]\nworkers = 2\ntick_sec = 60\n")
        self.approved_story("backlog")
        first = self.file_ready()

        def witness_then_merge():
            self.step()
            self.take(first)
            self.git("commit", "-q", "--allow-empty", "-m", "lands b")

        pool = self.schedule([(PARKED, witness_then_merge), (0, None),
                              (0, self.step)])

        self.assertEqual(self.modes(pool),
                         ["--worker", "--witness-step", "--witness-step"])
        self.assertNotEqual(self.tip(), self.base)
        self.assertEqual(self.read(LEDGER), [
            ("W1", self.base, "absent", "loop"),
            ("W1", self.tip(), "absent", "loop")])

    def test_a_held_witness_lock_skips_the_pass_until_it_is_released(self):
        self.approved_story("backlog")
        path = witness_lock_path(self.project)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out = self.step()

        self.assertIn("another witness pass holds the witness lock; skipping",
                      out)
        self.assertEqual(self.read(LEDGER), [])
        self.step()
        self.assertEqual(self.read(LEDGER), [("W1", self.base, "absent", "loop")])

    def test_the_witness_step_mode_runs_a_pass_in_its_own_process(self):
        self.approved_story("backlog")
        done = subprocess.run(
            [sys.executable, "factory.py", "--witness-step", str(self.target)],
            cwd=ROOT, capture_output=True, text=True, timeout=120)

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.read(LEDGER), [("W1", self.base, "absent", "loop")])

    def test_the_sweep_starts_a_loop_once_for_a_pending_pass(self):
        parent = self.approved_story("backlog")

        self.assertEqual(self.sweep(), [[(parent, None)]])
        self.step()
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.read(LEDGER), [("W1", self.base, "absent", "loop")])

    def test_a_held_project_neither_starts_a_loop_nor_runs_a_pass(self):
        self.approved_story("backlog")
        store.hold(self.conn, self.project_id, "maintenance")

        self.assertEqual(self.sweep(), [])
        self.step()
        self.assertEqual(self.read(LEDGER), [])

    def test_the_step_names_the_running_set_and_removes_it_after(self):
        copy = Path(self.target).parent / "seen.json"
        parent = self.approved_story("backlog", command=(
            f"cp {shlex.quote(str(self.record()))} {shlex.quote(str(copy))}"))
        (self.target / W1_FILE).write_text(PASSES)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "lands w1")
        (identifier,) = self.conn.execute(
            "SELECT linearIdentifier FROM tickets WHERE id = ?",
            (parent,)).fetchone()
        before = int(time.time() * 1000)
        out = self.step()
        after = int(time.time() * 1000)

        seen = json.loads(copy.read_text())
        self.assertEqual((seen["story"], seen["keys"], seen["pid"]),
                         (identifier, ["W1"], os.getpid()), out)
        self.assertTrue(before <= seen["started_ms"] <= after, seen)
        self.assertFalse(self.record().exists())

    def test_a_pass_that_raises_leaves_no_running_record(self):
        self.approved_story("backlog")
        with patch("holophyte.story.witness.witness_pass",
                   side_effect=RuntimeError("no tip")):
            out = self.step()

        self.assertIn("the witness pass could not run (no tip)", out)
        self.assertFalse(self.record().exists())

    def test_a_step_that_finds_the_lock_held_leaves_the_record_alone(self):
        self.approved_story("backlog")
        mark_running(self.project, "NAT-9", ["W2", "W3"])
        written = self.record().read_text()
        path = witness_lock_path(self.project)
        with open(path, "a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out = self.step()

        self.assertIn("another witness pass holds the witness lock; skipping",
                      out)
        self.assertEqual(self.record().read_text(), written)
