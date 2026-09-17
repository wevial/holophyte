"""A migrated store must not silence a live loop (KO-464)."""
import io
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import store
from holophyte import operator, pool, runs
from holophyte.config_tables import loop_config
from tests.loop_fixture import FakePool, LoopFixture, StubProvider, a_task


class HeartbeatSchemaBumpTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'store.sqlite3'
        self.conn = store.open(self.path)
        self.addCleanup(self.conn.close)
        project = store.ensure_project(self.conn, 'team', '/repos/example')
        ticket = store.mirror_ticket(self.conn, project, 'issue', 'KO-1', 'test')
        self.run = store.claim(self.conn, project, ticket, now=1000)

    def bump(self):
        self.conn.execute(f'PRAGMA user_version = {store.SCHEMA_VERSION + 1}')

    def three_beats(self, failure=None):
        """Real timer thread and writes, with three deterministic clock ticks."""
        done = threading.Event()
        beat = runs._beat
        heartbeat = store.heartbeat
        observed, connections, fallbacks = [], [], []
        clock = [2.0]
        out = io.StringIO()

        def record(conn, run_id, **kwargs):
            result = heartbeat(conn, run_id, **kwargs)
            if threading.current_thread() is not threading.main_thread():
                connections.append(conn)
            return result

        def timer(path, run_id, interval, stop, *args):
            ticks = 0

            def wait(seconds):
                nonlocal ticks
                if ticks:
                    with sqlite3.connect(self.path) as reader:
                        observed.append(reader.execute(
                            'SELECT lastHeartbeat FROM runs WHERE id = ?',
                            (self.run,)).fetchone()[0])
                if ticks == 3:
                    done.set()
                    return stop.wait(5)
                ticks += 1
                clock[0] += 1
                return False

            fallback = Mock(wraps=args[-1])
            fallbacks.append(fallback)
            beat(path, run_id, interval, Mock(wait=wait), *args[:-1], fallback)

        with redirect_stdout(out), patch.object(runs, '_beat', timer), \
                patch.object(store, 'heartbeat', record), \
                patch('store.time.time', side_effect=lambda: clock[0]):
            if failure:
                with patch.object(store, 'open', side_effect=failure):
                    with runs.heartbeat_while(self.conn, self.run, 10):
                        self.assertTrue(done.wait(5), 'heartbeat thread died')
            else:
                with runs.heartbeat_while(self.conn, self.run, 10):
                    self.assertTrue(done.wait(5), 'heartbeat thread died')
        self.assertEqual(observed, [3000, 4000, 5000])
        self.assertEqual(fallbacks[0].call_count,
                         3 if connections[0] is self.conn else 0)
        return connections, out.getvalue()

    def test_newer_schema_beats_through_existing_connection_and_reports_once(self):
        self.bump()
        with self.assertRaises(SystemExit) as caught:
            store.open(self.path)
        self.assertIsInstance(caught.exception, store.SchemaNewer)
        connections, output = self.three_beats()
        self.assertTrue(all(conn is self.conn for conn in connections))
        self.assertEqual(output.count('[holo2] heartbeat failed:'), 1)
        self.assertIn(f'version {store.SCHEMA_VERSION + 1} is newer', output)

    def test_missing_path_also_keeps_beating(self):
        connections, output = self.three_beats(
            sqlite3.OperationalError('fake path: unable to open database file'))
        self.assertTrue(all(conn is self.conn for conn in connections))
        self.assertIn('[holo2] heartbeat failed: fake path', output)

    def test_healthy_store_uses_only_the_threads_connection(self):
        connections, output = self.three_beats()
        self.assertTrue(all(conn is not self.conn for conn in connections))
        self.assertEqual(output, '')

    def test_fallback_waits_for_the_callers_transaction(self):
        self.bump()
        attempted, completed = threading.Event(), threading.Event()
        heartbeat = runs._fallback_heartbeat

        def fallback(*args):
            attempted.set()
            result = heartbeat(*args)
            completed.set()
            return result

        # Keep the beat context alive until after the caller commits, so its
        # thread can finish the blocked beat before the context joins it.
        with ExitStack() as stack, redirect_stdout(io.StringIO()), \
                patch.object(runs, '_fallback_heartbeat', fallback), \
                patch('store.time.time', return_value=5):
            with store.transaction(self.conn):
                stack.enter_context(runs.heartbeat_while(
                    self.conn, self.run, 0.001))
                self.assertTrue(attempted.wait(5))
                self.assertFalse(completed.wait(0.05),
                                 'fallback joined another thread\'s transaction')
            self.assertTrue(completed.wait(5))
            with sqlite3.connect(self.path) as reader:
                self.assertEqual(reader.execute(
                    'SELECT lastHeartbeat FROM runs WHERE id = ?',
                    (self.run,)).fetchone()[0], 5000)

    def test_fallback_shutdown_inside_callers_transaction(self):
        self.bump()
        attempted = threading.Event()
        beat, join = runs._beat, threading.Thread.join
        threads = []
        on_swept = Mock()

        def timer(path, run_id, interval, stop, swept, callback, heartbeat):
            def fallback():
                attempted.set()
                return heartbeat()
            beat(path, run_id, interval, stop, swept, callback, fallback)

        def bounded_join(thread):
            # Bound a broken shutdown so the regression fails instead of
            # hanging the suite; unwind the transaction before final cleanup.
            threads.append(thread)
            join(thread, timeout=1)
            self.assertFalse(thread.is_alive(), 'heartbeat shutdown deadlocked')

        try:
            with redirect_stdout(io.StringIO()), \
                    patch.object(runs, '_beat', timer), \
                    patch.object(threading.Thread, 'join', bounded_join):
                with store.transaction(self.conn):
                    with runs.heartbeat_while(
                            self.conn, self.run, 0.001, on_swept):
                        self.assertTrue(attempted.wait(5))
                on_swept.assert_not_called()
        finally:
            for thread in threads:
                join(thread, timeout=5)


class LoopSchemaBumpTests(LoopFixture):
    def test_pass_reexecutes_before_claiming_when_store_moves(self):
        conn = runs.open_store(self.tgt)
        self.addCleanup(conn.close)
        conn.execute(f'PRAGMA user_version = {store.SCHEMA_VERSION + 1}')
        provider = StubProvider(a_task())
        out = io.StringIO()
        with patch.object(operator, 'open_store', return_value=conn), \
                patch('holophyte.dispatch._startup_sweep', return_value=set()), \
                patch.object(operator, '_reconcile_at_startup'), \
                patch.object(operator, '_claim_next') as claim, \
                patch.object(operator, 'EXEC') as execute, redirect_stdout(out):
            operator._serial(self.tgt, provider, loop_config(self.tgt))
        execute.assert_called_once()
        claim.assert_not_called()
        self.assertIn(f'store schema moved to {store.SCHEMA_VERSION + 1}'
                      ' under this process; re-executing', out.getvalue())

    def test_schema_move_drains_live_workers_before_reexec(self):
        self.configure('[loop]\nworkers = 2\n')
        provider = StubProvider(*(a_task(n) for n in range(1, 4)))

        def bump():
            with sqlite3.connect(self.db) as conn:
                conn.execute(f'PRAGMA user_version = {store.SCHEMA_VERSION + 1}')

        workers = FakePool([(pool.WORKER_PARKED, bump),
                            (pool.WORKER_PARKED, None)])

        def execute(*args):
            self.assertEqual(workers.alive, [])

        out = io.StringIO()
        with patch.object(pool, 'SPAWN', workers.spawn), \
                patch.object(pool, 'WAIT', workers.wait), \
                patch.object(operator, 'EXEC', side_effect=execute) as reexec, \
                redirect_stdout(out):
            operator.main(self.tgt, provider)
        reexec.assert_called_once()
        self.assertEqual(len(workers.spawned), 2)
        self.assertEqual(len(workers.reaped), 2)
        self.assertIn(f'store schema moved to {store.SCHEMA_VERSION + 1}'
                      ' under this process; re-executing', out.getvalue())
