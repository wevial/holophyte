from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
# Resolve the harness equally under discovery and explicit module selection.
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    Commit,
    FakeAgent,
    Idle,
    no_agent_processes,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    TICK,
    FakePool,
    LoopFixture,
    Refuse,
    StubProvider,
    a_task,
)
from pool_restart_cases import PoolRestartCases  # noqa: E402

import holophyte.agents  # noqa: E402 - after the sys.path insert above
import holophyte.board  # noqa: E402 - after the sys.path insert above
import holophyte.config  # noqa: E402 - after the sys.path insert above
import holophyte.config_tables  # noqa: E402 - after the sys.path insert above
import holophyte.findings  # noqa: E402 - after the sys.path insert above
import holophyte.gates  # noqa: E402 - after the sys.path insert above
import holophyte.loop  # noqa: E402 - after the sys.path insert above
import holophyte.merge_gate  # noqa: E402 - after the sys.path insert above
import holophyte.operator  # noqa: E402 - after the sys.path insert above
import holophyte.pool  # noqa: E402 - after the sys.path insert above
import holophyte.runs  # noqa: E402 - after the sys.path insert above
import holophyte.supervisor  # noqa: E402 - after the sys.path insert above
import linear_provider  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets as tickets  # noqa: E402 - after the sys.path insert above


class GateConflictRequeueTests(LoopFixture):
    """A merge-gate conflict preserves and requeues the candidate."""

    def park_on_conflict(self, provider):
        """KO-131 parked by `_sync_main_into_branch()` on a README conflict;
        returns `(ticket_id, run_id)`."""
        branch = "task/ko-131"
        self.git("checkout", "-q", "-b", branch)
        (self.target / "README.md").write_text("branch\n")
        self.git("commit", "-qam", "branch side")
        self.git("checkout", "-q", "main")
        (self.target / "README.md").write_text("main\n")
        self.git("commit", "-qam", "main side")
        wt = self.worktrees / "ko-131"
        self.git("worktree", "add", "-q", str(wt), branch)
        sha = self.git("rev-parse", branch, cwd=wt).strip()
        conn = store.open(str(self.db))
        try:
            project = tickets.ensure_project(conn, StubProvider.TEAM,
                                           str(self.target))
            ticket = holophyte.board.mirror_task(conn, project, a_task())
            run_id = store.claim(conn, project, ticket)
            tickets.transition(conn, ticket, "in_flight")
            store.set_branch(conn, run_id, branch)
            # The conflict goes to the implementer first now (KO-404);
            # this fake leaves it unresolved, so the park is as before.
            with patch.object(holophyte.loop, "agent", FakeAgent(Idle())):
                with self.assertRaises(holophyte.gates.RunFailure) as failed:
                    holophyte.merge_gate._sync_main_into_branch(
                        self.tgt, conn, run_id, provider, "KO-131", branch,
                        wt, sha, 60, "add a thing", 5)
            holophyte.board.close_out_failure(
                self.tgt, conn, run_id, ticket, reason=str(failed.exception),
                provider=provider, refresh=False)
        finally:
            conn.close()
        return ticket, run_id

    def test_a_gate_conflict_park_is_requeued_with_its_note(self):
        provider = StubProvider(a_task())
        ticket, run_id = self.park_on_conflict(provider)
        self.assertEqual(
            self.read("SELECT status, activeRunId FROM tickets"),
            [("blocked_on_operator", None)])
        self.assertIn("merge conflict with main on: README.md",
                      self.read("SELECT blockedQuestion FROM tickets")[0][0])
        self.assertIn("README.md", self.read(
            "SELECT outcomeReason FROM runs WHERE outcome = 'failed'")[0][0])

        out = io.StringIO()
        holophyte.operator.requeue(self.tgt, "KO-131",
                               "resolved README.md on the branch", out)

        self.assertEqual(out.getvalue().strip(),
                         f"[holo2] KO-131 requeued after run {run_id}")
        self.assertEqual(
            self.read("SELECT status, blockedQuestion, activeRunId"
                      " FROM tickets"),
            [("ready", None, None)])
        self.assertEqual(
            self.read("SELECT runId, action FROM interventions"),
            [(run_id, "requeue")])
        noted = self.read(
            "SELECT summary FROM runEvents WHERE summary LIKE"
            " '%resolved README.md on the branch%'")
        self.assertEqual(len(noted), 1, noted)

    def test_a_pull_request_park_is_still_refused(self):
        provider = StubProvider(a_task())
        url = "https://github.com/example/repo/pull/7"
        conn = store.open(str(self.db))
        try:
            project = tickets.ensure_project(conn, StubProvider.TEAM,
                                           str(self.target))
            ticket = holophyte.board.mirror_task(conn, project, a_task())
            run_id = store.claim(conn, project, ticket)
            tickets.transition(conn, ticket, "in_flight")
            store.park(conn, run_id, "awaiting_merge_approval",
                       pr_url=url, candidate_sha="a" * 40)
            self.assertTrue(holophyte.board.block_ticket(
                conn, ticket, provider, f"PR open: {url}"))
        finally:
            conn.close()

        with self.assertRaises(SystemExit) as refused:
            holophyte.operator.requeue(self.tgt, "KO-131", "why not",
                                   io.StringIO())

        self.assertEqual(
            str(refused.exception),
            "[holo2] KO-131 is blocked_on_operator, not in_flight; nothing"
            " to requeue")
        self.assertEqual(
            self.read("SELECT status, blockedQuestion FROM tickets"),
            [("blocked_on_operator", f"PR open: {url}")])
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"),
                         [(0,)])


class PoolTests(PoolRestartCases, LoopFixture):
    """`[loop] workers > 1`: the main process schedules a pool of
    `--worker` children sized to the claimable queue (KO-343); spawn and
    wait go through seams, so no process is started."""
    def run_scheduler(self, workers, provider, exits, stop_on_failure=True,
                      tick_sec=None):
        tick = f"tick_sec = {tick_sec}\n" if tick_sec is not None else ""
        self.configure(f"[loop]\nworkers = {workers}\n"
                       f"stop_on_failure = {str(stop_on_failure).lower()}\n"
                       + tick)
        pool = FakePool(exits)
        out = io.StringIO()
        with patch.object(holophyte.pool, "SPAWN", pool.spawn), \
                patch.object(holophyte.pool, "WAIT", pool.wait), \
                patch.object(sys, "orig_argv",
                             ["python3", "-u", "factory.py", str(self.target)]), \
                patch.object(sys, "stdout", out):
            self.rc = holophyte.operator.main(self.tgt, provider)
        self.out = out.getvalue()
        return pool

    def test_the_pool_follows_the_claimable_queue_up_to_the_ceiling(self):
        """Five ready tickets under `workers = 3`: three workers at once, a
        fourth when one exits with four still claimable, and the scheduler
        exits 0 once the listing is empty and the last child is in."""
        provider = StubProvider(*(a_task(n) for n in range(1, 6)))

        def merged_one():
            provider.queue.pop(0)

        def merged_the_rest():
            provider.queue.clear()

        pool = self.run_scheduler(3, provider, [
            (holophyte.pool.WORKER_MERGED, merged_one),
            (holophyte.pool.WORKER_MERGED, merged_the_rest),
            (holophyte.pool.WORKER_MERGED, None),
            (holophyte.pool.WORKER_MERGED, None),
        ])

        # Three at the first tick, one more at the second, none after the
        # listing emptied: four spawns, every one waited for.
        self.assertEqual(len(pool.spawned), 4)
        self.assertEqual(len(pool.reaped), 4)
        self.assertEqual(pool.alive, [])
        self.assertIsNone(self.rc)
        # Each child is this command line plus `--worker`, with the slot in
        # its environment for the `[holo2 wN]` prefix.
        self.assertEqual([argv[1:] for argv in pool.spawned],
                         [["-u", "factory.py", str(self.target), "--worker"]] * 4)
        self.assertEqual([env[holophyte.pool.WORKER_SLOT_ENV]
                          for env in pool.envs], ["1", "2", "3", "4"])
        self.assertIn("[holo2] Linear has no ready tickets. done.", self.out)
        # The exit note a re-exec'd scheduler leaves for the sweep.
        self.assertEqual(self.read("SELECT COUNT(*) FROM loopRestarts"), [(0,)])

    def test_a_timer_tick_with_a_slot_free_spawns_for_a_ticket_filed_since(self):
        """A timer tick fills a free slot with newly ready work."""
        provider = StubProvider(a_task(1))
        conn = holophyte.runs.open_store(self.tgt)
        self.addCleanup(conn.close)
        project = tickets.ensure_project(conn, provider.team, self.target)

        def filed_one():
            # Worker 1 holds ticket 1; ticket 2 arrives on the board.
            store.claim(conn, project,
                        holophyte.board.mirror_task(conn, project, a_task(1)))
            conn.commit()
            provider.queue.append(a_task(2))

        pool = self.run_scheduler(3, provider, [
            (TICK, filed_one),
            (holophyte.pool.WORKER_MERGED, provider.queue.clear),
            (holophyte.pool.WORKER_MERGED, None),
        ], tick_sec=45)

        self.assertEqual(len(pool.spawned), 2)
        self.assertEqual(len(pool.reaped), 2)
        self.assertEqual(pool.timeouts, [45, 45, 45])
        self.assertIsNone(self.rc)
        # The tick itself printed nothing; only the spawn it made shows.
        # queue.clear() emptied the board without a claim, so the drain's
        # mirror reconcile (KO-425) walks KO-132's `ready` row to
        # `blocked_on_deps`.
        self.assertEqual(self.out.splitlines()[1:], [
            "[holo2] started worker 1 as pid 5001",
            "[holo2] started worker 2 as pid 5002",
            "[holo2] worker 1 merged its ticket",
            "[holo2] worker 2 merged its ticket",
            "[holo2] 1 mirror rows left the board's ready column; waiting"
            " on the board: KO-132",
            "[holo2] Linear has no ready tickets. done.",
        ])

    def test_a_full_pool_waits_on_exits_alone(self):
        """Three ready tickets under `workers = 3`: the pool is full, so
        the wait carries no timeout; once one exits with the listing
        emptied, two slots are free and the timer is back (KO-353)."""
        provider = StubProvider(*(a_task(n) for n in range(1, 4)))

        pool = self.run_scheduler(3, provider, [
            (holophyte.pool.WORKER_MERGED, provider.queue.clear),
            (holophyte.pool.WORKER_MERGED, None),
            (holophyte.pool.WORKER_MERGED, None),
        ])

        self.assertEqual(len(pool.spawned), 3)
        self.assertEqual(pool.timeouts, [None, 120, 120])
        self.assertIsNone(self.rc)

    def test_the_pool_refills_while_live_workers_hold_their_leases(self):
        """Live leases do not prevent free slots from being refilled."""
        provider = StubProvider(*(a_task(n) for n in range(1, 6)))
        conn = holophyte.runs.open_store(self.tgt)
        self.addCleanup(conn.close)
        project = tickets.ensure_project(conn, provider.team, self.target)

        def first_exit():
            # Worker 1 merged ticket 1; workers 2 and 3 hold tickets 2 and 3.
            provider.queue.pop(0)
            for n in (2, 3):
                store.claim(conn, project,
                            holophyte.board.mirror_task(conn, project, a_task(n)))
            conn.commit()

        pool = self.run_scheduler(3, provider, [
            (holophyte.pool.WORKER_MERGED, first_exit),
            (holophyte.pool.WORKER_MERGED, provider.queue.clear),
            (holophyte.pool.WORKER_MERGED, None),
            (holophyte.pool.WORKER_MERGED, None),
        ])

        # Three at the first tick, a fourth at the second: two free tickets
        # and two live workers make a pool of three, one short.
        self.assertEqual(len(pool.spawned), 4)
        self.assertIsNone(self.rc)

    def test_a_leased_ticket_is_not_counted_as_claimable(self):
        """Two ready tickets, one already held by a live run on this
        target: one worker, not two."""
        provider = StubProvider(a_task(1), a_task(2))
        conn = holophyte.runs.open_store(self.tgt)
        self.addCleanup(conn.close)
        project = tickets.ensure_project(conn, provider.team, self.target)
        ticket = holophyte.board.mirror_task(conn, project, a_task(1))
        store.claim(conn, project, ticket)

        pool = self.run_scheduler(3, provider, [
            (holophyte.pool.WORKER_MERGED, provider.queue.clear),
        ])

        self.assertEqual(len(pool.spawned), 1)
        self.assertIsNone(self.rc)

    def test_the_claimable_count_is_one_store_read_per_tick(self):
        """Count claimable tickets with one store read per tick."""
        provider = StubProvider(*(a_task(n) for n in range(1, 6)))
        conn = holophyte.runs.open_store(self.tgt)
        self.addCleanup(conn.close)
        project = tickets.ensure_project(conn, provider.team, self.target)
        ids = [holophyte.board.mirror_task(conn, project, a_task(n))
               for n in range(1, 6)]
        for ticket in ids[2:]:
            conn.execute("UPDATE tickets SET dependsOn = ? WHERE id = ?",
                         (json.dumps([a_task(1)["issue_id"]]), ticket))
        conn.commit()
        statements = []
        conn.set_trace_callback(statements.append)
        self.addCleanup(conn.set_trace_callback, None)

        counted = holophyte.pool._claimable(conn, project, provider.queue)

        # Two claimable: the first two; the other three wait on the first.
        self.assertEqual(counted, 2)
        self.assertEqual(len(statements), 1, statements)

    def test_a_dependency_blocked_ticket_is_not_counted_as_claimable(self):
        """Two ready tickets, the second depending on the first, which is not
        merged: one worker, not two. The count asks the store's own
        `pickable()`, dependencies included, not status and lease alone."""
        provider = StubProvider(a_task(1), a_task(2))
        conn = holophyte.runs.open_store(self.tgt)
        self.addCleanup(conn.close)
        project = tickets.ensure_project(conn, provider.team, self.target)
        holophyte.board.mirror_task(conn, project, a_task(1))
        second = holophyte.board.mirror_task(conn, project, a_task(2))
        conn.execute("UPDATE tickets SET dependsOn = ? WHERE id = ?",
                     (json.dumps([a_task(1)["issue_id"]]), second))
        conn.commit()

        pool = self.run_scheduler(3, provider, [
            (holophyte.pool.WORKER_MERGED, lambda: provider.queue.pop(0)),
        ])

        self.assertEqual(len(pool.spawned), 1)
        self.assertIsNone(self.rc)

    def test_a_listing_failure_is_not_an_empty_queue(self):
        """The board cannot be asked: nothing is spawned, and the scheduler
        exits nonzero rather than reporting an empty queue it never saw."""
        provider = StubProvider(a_task(1), a_task(2))

        def down():
            raise RuntimeError("linear unreachable")

        provider.ready_issues = down

        pool = self.run_scheduler(2, provider, [])

        self.assertEqual(pool.spawned, [])
        self.assertEqual(self.rc, 1)
        self.assertNotIn("Linear has no ready tickets", self.out)
        self.assertIn("linear unreachable", self.out)

    def test_mirror_that_spends_budget_does_not_spawn_workers(self):
        budget = linear_provider.LinearBudget()
        provider = StubProvider(a_task(1))
        ready = provider.ready_issues

        def listing():
            budget.remember({
                "x-ratelimit-complexity-limit": "3000000",
                "x-ratelimit-complexity-remaining": "0",
                "x-ratelimit-complexity-reset": "9999999999999"})
            return ready()

        provider.ready_issues = listing
        with patch.object(linear_provider, "LINEAR_BUDGET", budget):
            pool = self.run_scheduler(2, provider, [])
        self.assertEqual(pool.spawned, [])
        self.assertIn("board not asked: budget resets at", self.out)

    def test_a_low_complexity_budget_relists_nothing(self):
        """KO-434: low budget skips listing, names reset, and is not an empty queue."""
        budget = linear_provider.LinearBudget()
        budget.remember({
            "x-ratelimit-complexity-limit": "3000000",
            "x-ratelimit-complexity-remaining": "200000",
            "x-ratelimit-complexity-reset": "9999999999999"})
        provider = StubProvider(a_task(1), a_task(2))
        asked = []
        ready_issues = provider.ready_issues
        provider.ready_issues = lambda: asked.append(1) or ready_issues()

        with patch.object(linear_provider, "LINEAR_BUDGET", budget):
            pool = self.run_scheduler(2, provider, [])

        self.assertEqual(asked, [])
        self.assertEqual(pool.spawned, [])
        self.assertEqual(self.rc, 1)
        self.assertIn("board not asked: budget resets at", self.out)
        self.assertEqual(self.out.count("board not asked"), 1)
        self.assertNotIn("Linear has no ready tickets", self.out)

    def test_stop_on_failure_drains_the_pool_and_exits_nonzero(self):
        """`workers = 2`, `stop_on_failure = true`: a worker exits failed
        while another runs. No new worker is spawned though tickets remain,
        the running one is waited for, and the exit is nonzero."""
        provider = StubProvider(*(a_task(n) for n in range(1, 5)))

        pool = self.run_scheduler(2, provider, [
            (holophyte.pool.WORKER_FAILED, None),
            (holophyte.pool.WORKER_MERGED, None),
        ])

        self.assertEqual(len(pool.spawned), 2)
        self.assertEqual([code for _, code in pool.reaped],
                         [holophyte.pool.WORKER_FAILED,
                          holophyte.pool.WORKER_MERGED])
        self.assertEqual(pool.alive, [])
        self.assertEqual(self.rc, 1)
        self.assertIn("[holo2] worker 1 failed (exit 1)", self.out)

    def test_stop_on_failure_false_keeps_spawning_and_exits_nonzero(self):
        provider = StubProvider(*(a_task(n) for n in range(1, 4)))

        pool = self.run_scheduler(2, provider, [
            (holophyte.pool.WORKER_FAILED, None),
            (holophyte.pool.WORKER_MERGED, provider.queue.clear),
            (holophyte.pool.WORKER_MERGED, None),
        ], stop_on_failure=False)

        self.assertEqual(len(pool.spawned), 3)
        self.assertEqual(self.rc, 1)

    def test_a_self_merge_re_execs_after_the_pool_drains(self):
        provider = StubProvider(*(a_task(n) for n in range(1, 5)))
        version = store.schema.SCHEMA_VERSION + 1
        events = []
        with patch.object(holophyte.operator, "EXEC",
                          lambda *args: events.append("EXEC")), \
                patch.object(holophyte.operator, "sh",
                             self.fetched_git(version, events)), \
                patch.object(holophyte.operator, "self_hosted", return_value=True):
            pool = self.run_scheduler(3, provider, [
                (holophyte.pool.WORKER_MERGED, lambda: events.append("exit")),
                (holophyte.pool.WORKER_MERGED, lambda: events.append("exit")),
                (holophyte.pool.WORKER_MERGED, lambda: events.append("exit")),
            ])

        self.assertEqual(events, ["exit", "fetch", "exit", "exit", "merge", "EXEC"])
        self.assertIn(f"schema {version - 1} -> {version}; draining 2 worker(s)",
                      self.out)
        self.assertEqual(len(pool.spawned), 3)

    def test_a_failure_under_stop_on_failure_is_not_lost_to_a_self_merge(self):
        """`workers = 2`, `stop_on_failure = true`, self-hosted: one worker
        fails and the other merges a change to the factory itself. The stop
        wins: no re-exec -- a restarted scheduler would spawn again and exit
        clean -- and the exit is nonzero."""
        provider = StubProvider(*(a_task(n) for n in range(1, 5)))
        execs = []
        with patch.object(holophyte.operator, "EXEC",
                          lambda *args: execs.append(args)), \
                patch.object(holophyte.operator, "__file__",
                             str(self.target / "holophyte" / "operator.py")):
            pool = self.run_scheduler(2, provider, [
                (holophyte.pool.WORKER_FAILED, None),
                (holophyte.pool.WORKER_MERGED, None),
            ])

        self.assertEqual(len(pool.spawned), 2)
        self.assertEqual(pool.alive, [])
        self.assertEqual(execs, [])
        self.assertEqual(self.rc, 1)
        self.assertEqual(self.read("SELECT COUNT(*) FROM loopRestarts"), [(0,)])

    def test_every_spawned_worker_is_reported_by_wait(self):
        """Real children through the real seams: a worker that exited before
        the next spawn is still reported by `WAIT`, with its own exit code.
        (The reviewer's reproduction: with only the pid kept, `Popen`'s
        housekeeping reaped the first child and the second wait raised.)"""
        children = {}  # pid -> Popen, as the scheduler hands them to WAIT
        script = ("import os, sys;"
                  f" sys.exit(int(os.environ['{holophyte.pool.WORKER_SLOT_ENV}']))")
        with patch.object(sys, "orig_argv", [sys.executable, "-c", script]), \
                patch.object(sys, "stdout", io.StringIO()):
            first = holophyte.pool._spawn_worker(self.tgt, 1)
            children[first.pid] = first
            # Exited but unreaped when the second is spawned.
            os.waitid(os.P_PID, first.pid, os.WEXITED | os.WNOWAIT)
            second = holophyte.pool._spawn_worker(self.tgt, 2)
            children[second.pid] = second
            reaped = {}
            for _ in range(2):
                pid, code = holophyte.pool.WAIT(children, None)
                reaped[children.pop(pid)] = code

        self.assertEqual(reaped, {first: 1, second: 2})
        self.assertEqual((first.returncode, second.returncode), (1, 2))


class WorkerTests(LoopFixture):
    """`--worker`: the serial loop's phases once, for one ticket."""

    def worker(self, *script, provider):
        fake = FakeAgent(*script)
        out = io.StringIO()
        with no_agent_processes(), \
                patch.dict(sys.modules, {"linear_provider": provider}), \
                patch.object(holophyte.loop, "agent", fake), \
                patch.dict(os.environ, {holophyte.pool.WORKER_SLOT_ENV: "2"}), \
                patch.object(sys, "stdout", out):
            rc = holophyte.pool.worker(self.tgt, provider)
        return rc, out.getvalue()

    def test_worker_checks_budget_before_claiming(self):
        provider = StubProvider(a_task(1))
        budget = linear_provider.LinearBudget()
        budget.remember({
            "x-ratelimit-complexity-limit": "3000000",
            "x-ratelimit-complexity-remaining": "0",
            "x-ratelimit-complexity-reset": "9999999999999"})
        provider.LINEAR_BUDGET = budget
        with patch("holophyte.claim._claim_next") as claim:
            rc, out = self.worker(provider=provider)
        claim.assert_not_called()
        self.assertEqual(rc, holophyte.pool.WORKER_IDLE)
        self.assertIn("board not asked: budget resets at", out)

    def test_a_worker_merges_one_ticket_and_exits_merged(self):
        provider = StubProvider(a_task(1), a_task(2))

        rc, out = self.worker(Commit("the scripted work"), APPROVE,
                              provider=provider)

        self.assertEqual(rc, holophyte.pool.WORKER_MERGED)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        # The second ticket is left for another worker.
        self.assertEqual([t["id"] for t in provider.queue], ["KO-132"])
        self.assertIn("Merge task/ko-131-add-a-thing: add a thing",
                      self.subjects())
        # Its lines carry the slot, in place of the bare tag.
        self.assertIn("[holo2 w2] ", out)
        self.assertNotIn("\n[holo2] ", "\n" + out)

    def test_a_worker_prefixes_its_stderr_too(self):
        """A worker that dies before its first line -- here the store will
        not open -- writes its traceback to stderr, the stream the pool
        shares with its siblings; every line of it carries the slot, or
        the log cannot say which worker died."""
        provider = StubProvider(a_task(1))
        err = io.StringIO()
        with patch.dict(os.environ, {holophyte.pool.WORKER_SLOT_ENV: "2"}), \
                patch.object(holophyte.pool, "open_store",
                             side_effect=RuntimeError("store locked")), \
                patch.object(sys, "stdout", io.StringIO()), \
                patch.object(sys, "stderr", err):
            try:
                holophyte.pool.worker(self.tgt, provider)
            except RuntimeError:
                # What the interpreter does with an exception that reaches
                # the top of a `--worker` process.
                sys.excepthook(*sys.exc_info())
            else:
                self.fail("the worker swallowed its store failure")
        lines = err.getvalue().splitlines()
        self.assertIn("RuntimeError: store locked", "\n".join(lines))
        self.assertTrue(all(line.startswith("[holo2 w2] ") for line in lines),
                        lines)

    def test_the_prefix_survives_indentation_written_on_its_own(self):
        """Some interpreters' `traceback` writes a source line's indentation
        and the line as two writes; the prefix must still open the line, or
        a traceback's source lines lose their slot in the shared log."""
        out = io.StringIO()
        prefixed = holophyte.pool._PrefixedOut(out, "[holo2 w2]")
        prefixed.write("    ")
        prefixed.write("raise RuntimeError\n")
        prefixed.write("[holo2] run failed\n")
        self.assertEqual(out.getvalue().splitlines(),
                         ["[holo2 w2]     raise RuntimeError",
                          "[holo2 w2] run failed"])

    def test_a_worker_commits_findings_under_the_merge_lock(self):
        """The FINDINGS.md regeneration and commit run while this worker
        holds the merge lock, so no sibling is merging in the checkout while
        the file is written and `git commit` runs."""
        self.configure('[report]\nfindings = "repo"\n')
        provider = StubProvider(a_task(1))
        lock = holophyte.gates.merge_lock_path(self.tgt)
        held = []

        def commit_under_lock(target, message):
            held.append(lock.exists())
            return holophyte.findings.commit_findings(target, message)

        with patch.object(holophyte.pool, "commit_findings", commit_under_lock), \
            patch.object(holophyte.merge_gate, "commit_findings", commit_under_lock):
            rc, _ = self.worker(Commit("the scripted work"), APPROVE,
                                provider=provider)

        self.assertEqual(rc, holophyte.pool.WORKER_MERGED)
        # Two commits in the run -- the gate's pre-merge one and the
        # close-out -- and the lock was held for each.
        self.assertEqual(held, [True, True])
        self.assertIn("Complete task KO-131: add a thing", self.subjects())
        self.assertFalse(lock.exists())  # and released after

    def test_a_failed_worker_renders_findings_under_the_merge_lock(self):
        """A failed run's close-out regenerates FINDINGS.md too, and a
        worker's does so under the merge lock: a sibling may be merging in
        the checkout at that moment (the review of KO-343)."""
        self.configure('[report]\nfindings = "repo"\n')
        provider = StubProvider(a_task(1))
        lock = holophyte.gates.merge_lock_path(self.tgt)
        held = []
        real = holophyte.findings.refresh_findings

        def render_under_lock(target, conn):
            held.append(lock.exists())
            return real(target, conn)

        with patch.object(holophyte.pool, "refresh_findings", render_under_lock), \
                patch.object(holophyte.board, "refresh_findings", render_under_lock):
            rc, _ = self.worker(Refuse(), provider=provider)

        self.assertEqual(rc, holophyte.pool.WORKER_FAILED)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        # Rendered once, with the lock held, and released after.
        self.assertEqual(held, [True])
        self.assertIn("KO-131", (self.tgt.path / "FINDINGS.md").read_text())
        self.assertFalse(lock.exists())

    def test_a_worker_with_nothing_to_claim_exits_idle(self):
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
        rc, out = self.worker(provider=StubProvider())
        self.assertEqual(out.splitlines()[0], f"[holo2 w2] factory at {sha}")
        self.assertEqual(rc, holophyte.pool.WORKER_IDLE)
        self.assertEqual(self.read("SELECT COUNT(*) FROM runs"), [(0,)])


class SweptHeartbeatTests(unittest.TestCase):
    """A swept heartbeat stops the worker without dispatching stale work."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"
        self.conn = store.open(self.path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = tickets.ensure_project(self.conn, "team_abc", "/repos/x")
        ticket = tickets.mirror_ticket(
            self.conn, project, "iss_1", "KO-1", "ticket one",
            acceptance_criteria=["it works"], verification_commands=["true"])
        self.run = store.claim(self.conn, project, ticket)

    def test_a_run_ended_mid_block_fires_the_callback_once_and_raises(self):
        reason = "swept by the supervisor in phase working: time_box (99 min)"
        calls = []
        fired = threading.Event()

        def on_swept():
            calls.append(time.monotonic())
            fired.set()

        with self.assertRaises(holophyte.runs.RunSwept) as caught:
            with holophyte.loop.heartbeat_while(self.conn, self.run, 0.05,
                                                on_swept=on_swept):
                other = store.open(self.path)
                try:
                    store.release(other, self.run, "failed", reason)
                finally:
                    other.close()
                self.assertTrue(fired.wait(5), "the beat never saw the end")
                # The block goes on past several more beat intervals: a
                # callback fired per beat would show up here as a second call.
                time.sleep(0.3)

        self.assertEqual(len(calls), 1)
        self.assertEqual(caught.exception.run_id, self.run)
        self.assertEqual(caught.exception.reason, reason)
        self.assertIn(f"run {self.run}", str(caught.exception))

    def test_a_run_ended_after_the_last_beat_still_raises_at_exit(self):
        # The sweep lands after the timer's last beat and the block returns
        # at once: no beat sees the end. The exit has to look for itself,
        # or the loop verifies and records against a run the store failed.
        reason = "swept by the supervisor in phase working: time_box (99 min)"
        calls = []
        with self.assertRaises(holophyte.runs.RunSwept) as caught:
            with holophyte.loop.heartbeat_while(self.conn, self.run, 60,
                                                on_swept=calls.append):
                other = store.open(self.path)
                try:
                    store.release(other, self.run, "failed", reason)
                finally:
                    other.close()
        self.assertEqual(calls, [])  # nothing left to stop: the body returned
        self.assertEqual(caught.exception.run_id, self.run)
        self.assertEqual(caught.exception.reason, reason)

    def test_a_live_run_raises_nothing_and_calls_nothing(self):
        calls = []
        with holophyte.loop.heartbeat_while(self.conn, self.run, 0.05,
                                            on_swept=calls.append):
            time.sleep(0.2)
        self.assertEqual(calls, [])


class SweptTurnTests(LoopFixture):
    """A swept turn stops its worker and preserves the candidate."""

    def test_a_swept_run_kills_its_turn_writes_nothing_more_and_moves_on(self):
        # A 600 ms stale threshold gives a 300 ms beat to detect the sweep.
        self.configure("[supervisor]\nheartbeat_stale_min = 0.01\n")
        knobs = holophyte.config_tables.sweep_config(self.tgt)
        db, tgt = self.db, self.tgt
        seen = {}
        fake = FakeAgent()

        class BlockUntilKilled(Idle):
            """An implementer that sweeps its own run from outside, then
            blocks in a process only a kill of its group can end."""

            def play(self, cwd, turn):
                proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
                fake.turns[-1].on_start(proc)
                conn = store.open(str(db))
                try:
                    (run_id,) = conn.execute("SELECT id FROM runs").fetchone()
                    # The fake agent has spent an hour of work.
                    conn.execute("UPDATE runs SET workingMs = 3600000")
                    conn.commit()
                    result = holophyte.supervisor.sweep(
                        tgt, conn, int(time.time() * 1000) + 3_600_000,
                        act=True, knobs=knobs)
                    seen["trips"] = [t.condition for t in result.trips]
                    seen["row"] = conn.execute(
                        "SELECT outcome, outcomeReason, endedAt FROM runs"
                        " WHERE id = ?", (run_id,)).fetchone()
                    seen["events"] = conn.execute(
                        "SELECT COUNT(*) FROM runEvents WHERE runId = ?",
                        (run_id,)).fetchone()[0]
                    seen["rounds"] = conn.execute(
                        "SELECT COUNT(*) FROM reviewRounds WHERE runId = ?",
                        (run_id,)).fetchone()[0]
                finally:
                    conn.close()
                try:
                    proc.wait(timeout=20)
                    seen["returncode"] = proc.returncode
                except subprocess.TimeoutExpired:
                    # The loop never killed it: end it here so the test
                    # fails on the record below rather than hanging.
                    proc.kill()
                    proc.wait()
                    seen["returncode"] = "the loop never killed the turn"
                return "killed"

        fake.script = [BlockUntilKilled(), Commit("the next work"), APPROVE]
        provider = StubProvider(a_task(1), a_task(2))
        out = io.StringIO()
        with patch.object(sys, "stdout", out):
            self.loop(provider=provider, fake=fake)
        out = out.getvalue()

        # The sweep tripped the time box and ended the run.
        self.assertEqual(seen["trips"], ["time_box"])
        self.assertEqual(seen["row"][0], "failed")
        self.assertIn("swept by the supervisor", seen["row"][1])
        self.assertIsNotNone(seen["row"][2])
        # The turn's process was killed, not left to finish its 30 seconds.
        self.assertEqual(seen["returncode"], -signal.SIGKILL)
        self.assertIn("[holo2] run 1 was ended by the supervisor"
                      f" ({seen['row'][1]}); stopping this turn", out)
        # Nothing more was written to the swept run: its row and its
        # streams are as the sweep left them.
        self.assertEqual(
            self.read("SELECT outcome, outcomeReason, endedAt FROM runs"
                      " WHERE id = 1"), [seen["row"]])
        self.assertEqual(
            self.read("SELECT COUNT(*) FROM runEvents WHERE runId = 1"),
            [(seen["events"],)])
        self.assertEqual(
            self.read("SELECT COUNT(*) FROM reviewRounds WHERE runId = 1"),
            [(seen["rounds"],)])
        # Worktree and branch are as the sweep preserved them.
        self.assertIn("task/ko-131-add-a-thing", self.branches())
        self.assertTrue(any(p.is_dir() for p in self.worktrees.iterdir()))
        # The loop made its next claim and finished that run on its own.
        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        self.assertIn(("iss-132", "In Progress"), provider.states)
        self.assertEqual(self.read("SELECT id, outcome FROM runs ORDER BY id"),
                         [(1, "failed"), (2, "merged")])
        self.assertIn("the next work", self.subjects())
        self.assertIsNone(self.rc)


class ImplementerProbeTests(LoopFixture):
    def assert_unclaimed_route_down(self):
        conn = store.open(str(self.db))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone(), (0,))
            event, = conn.execute(
                "SELECT projectId, summary FROM runEvents WHERE kind='route_down'"
            ).fetchall()
            self.assertTrue(event[0] and "implementer probe failed" in event[1])
        finally:
            conn.close()

    def script(self, body):
        path = self.db.parent / "implementer.sh"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        self.configure(f'[agents]\nimplementer = "{path}"\n')
        return path

    def test_a_route_that_answers_ready_lets_the_pass_proceed(self):
        path = self.script('echo "ready"\n')
        out = self.main_output(Commit("work"), APPROVE)
        self.assertIn("implementer probe passed", out)
        self.assertIn(str(path), out)
        self.assertTrue(any(subject.startswith("Merge task/")
                            for subject in self.subjects()), self.subjects())
        self.assertIsNone(self.rc)

    def test_a_route_that_exits_nonzero_ends_the_pass_before_any_claim(self):
        path = self.script("echo broken harness >&2\nexit 1\n")
        out = self.main_output(Commit("work"), APPROVE)
        self.assertEqual(self.rc, 1)
        self.assertIn("implementer probe failed (exit 1)", out)
        self.assertIn(str(path), out)
        self.assertIn("broken harness", out)
        self.assert_unclaimed_route_down()
        self.assertEqual(self.last_provider.states, [])
        self.assertEqual(len(self.last_provider.queue), 1)
        self.assertEqual(self.last_fake.turns, [])

    def test_a_route_that_cannot_start_ends_the_pass_naming_the_reason(self):
        missing = self.db.parent / "no-such-harness"
        self.configure(f'[agents]\nimplementer = "{missing}"\n')
        out = self.main_output(Commit("work"), APPROVE)
        self.assertEqual(self.rc, 1)
        self.assertIn("implementer probe failed (could not start:", out)
        self.assertIn("No such file", out)
        self.assertIn(str(missing), out)
        self.assert_unclaimed_route_down()

    def test_a_route_that_hangs_past_the_cap_ends_the_pass_naming_it(self):
        path = self.script("sleep 30\n")
        with patch.object(holophyte.agents, "PROBE_TIMEOUT", 1):
            out = self.main_output(Commit("work"), APPROVE)
        self.assertEqual(self.rc, 1)
        self.assertIn("implementer probe failed (no answer within 1s)", out)
        self.assertIn(str(path), out)
        self.assert_unclaimed_route_down()
