"""A migrated store must not silence a live loop (KO-464)."""
import io
import json
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
from tests.loop_fixture import TICK, FakePool, LoopFixture, StubProvider, a_task
from tests.schema_fixture import move_ahead_additively


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

    def three_beats(self, failure=None, count=3):
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
                if ticks == count:
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
        self.assertEqual(observed, [3000 + 1000 * n for n in range(count)])
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

    def test_additive_bump_at_the_floor_keeps_the_threads_connection(self):
        move_ahead_additively(self.path, readableFrom=store.SCHEMA_VERSION)
        connections, output = self.three_beats()
        self.assertEqual(len(connections), 3)
        self.assertTrue(all(conn is not self.conn for conn in connections))
        self.assertNotIn('beating through the open connection', output)

    def test_three_failed_opens_then_recovery_keeps_every_beat(self):
        opened = store.open(self.path)
        self.addCleanup(opened.close)
        self.bump()
        try:
            store.open(self.path)
        except store.SchemaNewer as exc:
            failure = exc
        connections, output = self.three_beats(
            [failure, failure, failure, opened], count=4)
        self.assertEqual(connections, [self.conn] * 3 + [opened])
        self.assertEqual(output.count('heartbeat failed:'), 1)
        self.assertIn('; beating through the open connection', output)
        self.assertEqual(output.count('heartbeat recovered'), 1)

    def test_missing_path_also_keeps_beating(self):
        connections, output = self.three_beats(
            sqlite3.OperationalError('fake path: unable to open database file'))
        self.assertTrue(all(conn is self.conn for conn in connections))
        self.assertIn('[holo2] heartbeat failed: fake path', output)

    def test_open_and_beat_failures_share_an_episode_until_a_live_beat(self):
        for alive in (True, False):
            with self.subTest(alive=alive):
                opened = Mock()
                output = io.StringIO()
                snapshots = []
                on_swept = Mock()

                def recovered_connection_beat(*args):
                    snapshots.append(output.getvalue())
                    if len(snapshots) == 1:
                        raise sqlite3.OperationalError('write still unavailable')
                    return alive

                with redirect_stdout(output), \
                        patch.object(store, 'open', side_effect=[
                            sqlite3.OperationalError('open unavailable'), opened]), \
                        patch.object(runs, '_heartbeat',
                                     side_effect=recovered_connection_beat):
                    runs._beat(self.path, self.run, 1,
                               Mock(wait=Mock(side_effect=[False] * 3 + [True])),
                               [], on_swept, Mock(side_effect=
                                   sqlite3.OperationalError('fallback unavailable')))

                self.assertEqual(len(snapshots), 2)
                for snapshot in snapshots:
                    self.assertEqual(snapshot.count('heartbeat failed:'), 1)
                    self.assertNotIn('heartbeat recovered', snapshot)
                self.assertEqual(output.getvalue().count('heartbeat failed:'), 1)
                self.assertEqual(output.getvalue().count('heartbeat recovered'),
                                 int(alive))
                self.assertEqual(on_swept.call_count, int(not alive))
                opened.close.assert_called_once()

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
        conn = runs.open_store(self.project)
        self.addCleanup(conn.close)
        conn.execute(f'PRAGMA user_version = {store.SCHEMA_VERSION + 1}')
        provider = StubProvider(a_task())
        out = io.StringIO()
        with patch.object(operator, 'open_store', return_value=conn), \
                patch('holophyte.dispatch._startup_sweep', return_value=set()), \
                patch.object(operator, '_reconcile_at_startup'), \
                patch.object(operator, '_claim_next') as claim, \
                patch.object(operator, 'EXEC') as execute, redirect_stdout(out):
            operator._serial(self.project, provider, loop_config(self.project))
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
            operator.main(self.project, provider)
        reexec.assert_called_once()
        self.assertEqual(len(workers.spawned), 2)
        self.assertEqual(len(workers.reaped), 2)
        self.assertIn(f'store schema moved to {store.SCHEMA_VERSION + 1}'
                      ' under this process; re-executing', out.getvalue())

    def run_readable_move(self, exits, sh=None):
        """`workers = 3` over two ready tickets; `exits`' first callback
        moves the store one version ahead, readable from this build."""
        self.configure('[loop]\nworkers = 3\n')
        self.provider = StubProvider(a_task(1), a_task(2))
        workers = FakePool(exits)
        self.handed = []

        def execute(*args):
            self.handed.append((list(workers.alive), json.loads(
                self.project.store_path.with_name('pool.json').read_text())))

        out = io.StringIO()
        with ExitStack() as stack:
            if sh is not None:
                stack.enter_context(patch.object(operator, 'sh', sh))
            stack.enter_context(patch.object(pool, 'SPAWN', workers.spawn))
            stack.enter_context(patch.object(pool, 'WAIT', workers.wait))
            stack.enter_context(patch.object(operator, 'EXEC', execute))
            stack.enter_context(redirect_stdout(out))
            operator.main(self.project, self.provider)
        return workers, out.getvalue()

    def move(self):
        move_ahead_additively(self.db, readableFrom=store.SCHEMA_VERSION)

    def test_readable_move_hands_live_workers_to_the_new_build(self):
        version = store.SCHEMA_VERSION + 1
        original = operator.sh

        def fetched(args, cwd):
            if args[:2] == ['git', 'fetch'] or args[:2] == ['git', 'merge']:
                return ''
            if args == ['git', 'show', 'origin/main:store/schema.py']:
                return (f'SCHEMA_VERSION = {version}\n'
                        f'READABLE_FROM = {store.SCHEMA_VERSION}\n')
            if args == ['git', 'rev-parse', '--short', 'origin/main']:
                return 'new5678'
            return original(args, cwd)

        workers, out = self.run_readable_move([(TICK, self.move)], fetched)

        self.assertEqual(len(self.handed), 1)
        alive, handoff = self.handed[0]
        self.assertEqual(alive, [5001, 5002])
        self.assertEqual([(w['pid'], w['previous']) for w in handoff['workers']],
                         [(5001, True), (5002, True)])
        self.assertEqual(workers.reaped, [])
        self.assertIn(f'store schema moved to {version} under this process'
                      ' and is readable by this build; re-executing', out)
        self.assertIn(f'is additive (readable from {store.SCHEMA_VERSION})', out)

    def assert_stuck_checkout_keeps_spawning(self):
        def move_and_file_a_third():
            self.move()
            self.provider.queue.append(a_task(3))

        def finish():
            self.provider.queue.clear()

        workers, out = self.run_readable_move([
            (TICK, move_and_file_a_third), (TICK, None),
            (pool.WORKER_PARKED, finish), (pool.WORKER_PARKED, None),
            (pool.WORKER_PARKED, None)])

        self.assertEqual(self.handed, [])
        self.assertEqual(len(workers.spawned), 3)
        self.assertEqual(len(workers.reaped), 3)
        self.assertEqual(out.count('checkout not fast-forwarded'), 1)

    def test_readable_move_on_a_stuck_checkout_keeps_spawning(self):
        # The fixture's factory checkout has no origin: the fetch fails.
        self.assert_stuck_checkout_keeps_spawning()

    def test_readable_move_on_a_diverged_checkout_keeps_spawning(self):
        """Fetched, on main and clean, but the fast-forward itself fails."""
        origin = self.target.with_name('factory-origin')
        self.git('clone', '-q', str(self.target), str(origin))
        schema = origin / 'store' / 'schema.py'
        schema.parent.mkdir()
        schema.write_text(f'SCHEMA_VERSION = {store.SCHEMA_VERSION + 1}\n'
                          f'READABLE_FROM = {store.SCHEMA_VERSION}\n')
        identity = ('-c', 'user.email=factory@example.invalid',
                    '-c', 'user.name=Factory Test')
        self.git('add', '.', cwd=origin)
        self.git(*identity, 'commit', '-qm', 'additive bump', cwd=origin)
        self.git('remote', 'add', 'origin', str(origin))
        (self.target / 'LOCAL.md').write_text('a commit origin lacks\n')
        self.git('add', 'LOCAL.md')
        self.git('commit', '-qm', 'diverge')
        leaving = self.git('rev-parse', 'HEAD')

        self.assert_stuck_checkout_keeps_spawning()
        self.assertEqual(self.git('rev-parse', 'HEAD'), leaving)
