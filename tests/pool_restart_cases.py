"""Pool restart cases collected by test_pool and test_serve_runs."""
import json
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

        def update_checkout(_target):
            schema.write_text(f"SCHEMA_VERSION = {store.schema.SCHEMA_VERSION}\n")

        with patch.object(holophyte.operator, "_fast_forward_checkout",
                          side_effect=update_checkout) as update, \
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
            inherited, _ = holophyte.pool_handoff.restore(target)
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

