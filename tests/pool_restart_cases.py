"""Pool restart cases collected by test_pool and test_serve_runs."""
import io
import json
import os
import subprocess
import sys
from unittest.mock import patch

from loop_fixture import FakePool, StubProvider, a_task

import holophyte.operator
import holophyte.pool
import holophyte.pool_handoff
import holophyte.serve_runs
import holophyte.target
import store.schema


class PoolRestartCases:
    def fetched_git(self, version, events, floor=None):
        original_sh = holophyte.operator.sh
        schema = f'SCHEMA_VERSION = {version}\n'
        if floor is not None:
            schema += f'READABLE_FROM = {floor}\n'

        def fetched(args, cwd):
            if args[:2] == ['git', 'fetch']:
                events.append('fetch')
                return ''
            if args == ['git', 'show', 'origin/main:store/schema.py']:
                return schema
            if args == ['git', 'rev-parse', '--short', 'origin/main']:
                return 'new5678'
            if args[:2] == ['git', 'merge']:
                events.append('merge')
                return ''
            return original_sh(args, cwd)

        return fetched

    def write_schema(self, version):
        schema = self.target / "store" / "schema.py"
        schema.parent.mkdir(exist_ok=True)
        schema.write_text(f"SCHEMA_VERSION = {version}\n")

    def test_ordinary_merge_hands_three_children_to_the_new_build(self):
        provider = StubProvider(*(a_task(n) for n in range(1, 8)))
        self.configure("[loop]\nworkers = 4\n")
        fake = FakePool([(holophyte.pool.WORKER_MERGED, None)])
        execs = []
        schema = self.target / "store" / "schema.py"
        schema.parent.mkdir()
        schema.write_text(f"SCHEMA_VERSION = {store.schema.SCHEMA_VERSION + 1}\n")

        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        with patch.object(holophyte.operator, "_fetch_main",
                          return_value=True) as update, \
                patch.object(holophyte.pool_handoff, "fetched_schema",
                             return_value=(store.schema.SCHEMA_VERSION,
                                           store.schema.SCHEMA_VERSION)), \
                patch.object(holophyte.operator, "_ff_main"), \
                patch.object(holophyte.pool, "SPAWN", fake.spawn), \
                patch.object(holophyte.pool, "WAIT", fake.wait), \
                patch.object(holophyte.operator, "EXEC", lambda *a: execs.append(a)), \
                patch.object(holophyte.operator, "self_hosted", return_value=True):
            holophyte.operator.main(self.tgt, provider)
        update.assert_called_once()
        self.assertEqual(len(execs), 1)
        self.assertEqual(fake.alive, [5002, 5003, 5004])
        handoff = json.loads(self.tgt.store_path.with_name("pool.json").read_text())
        self.assertEqual([w["pid"] for w in handoff["workers"]], fake.alive)
        observed, counts = [], []

        def observe_count():
            counts.append(holophyte.serve_runs.workers_on_previous_build(self.tgt))

        def first_exit():
            observed.append(len(fake.spawned))
            observe_count()

        def finish_queue():
            observe_count()
            provider.queue.clear()

        fake.exits = [(holophyte.pool.WORKER_PARKED, first_exit),
                      (holophyte.pool.WORKER_PARKED, finish_queue),
                      (holophyte.pool.WORKER_PARKED, observe_count),
                      (holophyte.pool.WORKER_PARKED, observe_count)]
        self.configure("[loop]\nworkers = 3\n")
        with patch.object(holophyte.pool, "SPAWN", fake.spawn), \
                patch.object(holophyte.pool, "WAIT", fake.wait), \
                patch.object(holophyte.pool, "reexec_command",
                             return_value=("/new/python",
                                           ["/new/python", "factory.py"])):
            holophyte.operator.main(self.tgt, provider)
        self.assertEqual(observed, [4])
        self.assertEqual(counts, [3, 2, 1, 0])
        self.assertEqual(fake.spawned[-1], ["/new/python", "factory.py", "--worker"])
        handoff = json.loads(self.tgt.store_path.with_name("pool.json").read_text())
        self.assertEqual(handoff["workers"], [])


    def self_merge_under(self, version, floor, on_exec=None):
        """`workers = 3`, self-hosted: the first worker merges with the
        fetched schema at `version` readable from `floor`; returns the pool
        and the order of fetch, merge, EXEC and worker exits."""
        provider = StubProvider(*(a_task(n) for n in range(1, 5)))
        events = []

        def execute(*args):
            events.append("EXEC")
            if on_exec is not None:
                on_exec()

        with patch.object(holophyte.operator, "EXEC", execute), \
                patch.object(holophyte.operator, "sh",
                             self.fetched_git(version, events, floor)), \
                patch.object(holophyte.operator, "self_hosted", return_value=True):
            pool = self.run_scheduler(3, provider, [
                (holophyte.pool.WORKER_MERGED, lambda: events.append("exit")),
                (holophyte.pool.WORKER_MERGED, lambda: events.append("exit")),
                (holophyte.pool.WORKER_MERGED, lambda: events.append("exit")),
            ])
        return pool, events

    def test_a_self_merge_re_execs_after_the_pool_drains(self):
        version = store.schema.SCHEMA_VERSION + 1
        pool, events = self.self_merge_under(version, version)

        self.assertEqual(events, ["exit", "fetch", "exit", "exit", "merge", "EXEC"])
        self.assertIn(f"schema {version - 1} -> {version}; draining 2 worker(s)",
                      self.out)
        self.assertEqual(len(pool.spawned), 3)

    def test_a_fetched_schema_without_a_floor_still_drains(self):
        version = store.schema.SCHEMA_VERSION + 1
        pool, events = self.self_merge_under(version, None)

        self.assertEqual(events, ["exit", "fetch", "exit", "exit", "merge", "EXEC"])
        self.assertIn(f"schema {version - 1} -> {version}; draining 2 worker(s)",
                      self.out)

    def test_an_additive_self_merge_hands_live_workers_to_the_new_build(self):
        version = store.schema.SCHEMA_VERSION + 1
        handed = []

        def read_handoff():
            handoff = self.tgt.store_path.with_name("pool.json").read_text()
            handed.extend(json.loads(handoff)["workers"])

        pool, events = self.self_merge_under(version, version - 1, read_handoff)

        self.assertEqual(events, ["exit", "fetch", "merge", "EXEC"])
        self.assertEqual(pool.alive, [5002, 5003])
        self.assertEqual([(w["pid"], w["previous"]) for w in handed],
                         [(5002, True), (5003, True)])
        self.assertIn(f"schema {version - 1} -> {version} is additive (readable"
                      f" from {version - 1}); fast-forwarding to new5678 under"
                      " 2 live worker(s)", self.out)

    def test_a_failure_under_stop_on_failure_is_not_lost_to_a_self_merge(self):
        """`workers = 2`, `stop_on_failure = true`, self-hosted: one worker
        fails and the other merges a change to the factory itself. The stop
        wins: no re-exec -- a restarted scheduler would spawn again and exit
        clean -- and the stop still exits zero."""
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
        self.assertEqual(self.rc, 0)
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


class PreviousBuildCases:
    def test_previous_build_count_clears_when_inherited_child_is_reaped(self):
        self.seed()
        self.start()
        target = holophyte.target.Target.locate(self.target)
        child = subprocess.Popen([sys.executable, "-c",
                                  "import sys; sys.stdin.read()"],
                                 stdin=subprocess.PIPE)
        try:
            holophyte.pool_handoff.save(target, {child.pid: (1, child)})
            inherited = holophyte.pool_handoff.restore(target)
            code, _, body = self.request("GET", "/status")
            self.assertEqual(code, 200)
            self.assertEqual(body["workers_on_previous_build"], 1)
            child.stdin.close()
            pid, code = holophyte.pool._wait_any(
                {pid: worker for pid, (_, worker) in inherited.items()}, None)
            child.returncode = code
            self.assertEqual((pid, code), (child.pid, 0))
            inherited.pop(pid)
            holophyte.pool_handoff.save(target, inherited)
            self.assertEqual(self.request("GET", "/status")[2]
                             ["workers_on_previous_build"], 0)
        finally:
            child.stdin.close()
            if child.poll() is None:
                child.kill()
            child.wait()

