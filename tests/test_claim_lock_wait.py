"""Claim-time fetch contention, with accelerated lock time and real heartbeats."""
import contextlib
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import holophyte.claim as claim
import holophyte.gates as gates
import holophyte.merge_lock as merge_lock
import store
import store.read
from tests.sweep_fixture import SweepTestCase


class ClaimLockWaitTests(SweepTestCase):
    # Real milliseconds each poll oversleeps, as on a busy runner.
    delay_ms = 0

    def setUp(self):
        super().setUp()
        # A holder is live for 3 real seconds after its last beat, so a slow
        # poll on a busy runner cannot make it look stale (KO-669).
        self.configure('[merge]\ncheck_wait_sec = 300\n'
                       '[supervisor]\nheartbeat_stale_min = 0.05\n')
        self.holder = self.a_run(phase='merge_gate')
        self.waiter = self.a_run()
        self.path = gates.merge_lock_path(self.tgt)
        self.path.write_text(f'{self.holder} {time.time()}\n')
        self.release_from = None
        self.live = True
        self.beats = []
        self.phases = []
        self.started = time.monotonic()
        # 180 lock seconds take 180ms; heartbeat timers keep real time.
        for name in ('holophyte.gates.monotonic',
                     'holophyte.merge_lock.monotonic'):
            self.enterContext(patch(name, self.elapsed))
        self.enterContext(patch('holophyte.gates.MERGE_LOCK_POLL_SEC', 10))
        self.enterContext(patch('holophyte.merge_lock.CHECK_POLL_S', 10))
        self.enterContext(patch('holophyte.gates.sleep', self.poll))
        # The waiter still beats every 30ms, so a wait of a few hundred
        # milliseconds shows its heartbeat moving.
        beat = merge_lock.heartbeat_while
        self.enterContext(patch.object(
            merge_lock, 'heartbeat_while',
            lambda conn, run_id, interval_s: beat(conn, run_id, 0.03)))
        self.enterContext(patch.object(claim, 'sh', self.git))
        self.fetch = self.enterContext(patch.object(
            claim.subprocess, 'run', side_effect=self.fetched))

    def elapsed(self):
        return (time.monotonic() - self.started) * 1000

    def within(self, expected):
        """`expected` lock ms, one delayed poll and 500ms of scheduling."""
        return expected + 10 + self.delay_ms + 500

    def poll(self, seconds):
        if self.live:
            store.heartbeat(self.conn, self.holder)
        # Sampled at both ends, so even two slow polls show a moving beat.
        self.beats.append(self.last_beat())
        time.sleep((seconds + self.delay_ms) / 1000)
        self.beats.append(self.last_beat())
        self.phases.append(store.run_phase(self.conn, self.waiter))
        # Released only once the waiter is waiting past the default: a
        # slow start must not free the lock before the wait begins (KO-671).
        if (self.release_from is not None and self.events()
                and self.elapsed() >= self.release_from):
            self.path.unlink()
            self.release_from = None

    def last_beat(self):
        return store.read.run_snapshot(self.conn, self.waiter).lastHeartbeat

    def assert_waiter_beat(self, now_ms=None):
        # Three distinct beats, even over slow polls; a silent waiter's
        # phase writes show two.
        self.assertGreaterEqual(len(set(self.beats)), 3)
        # Fresh within one slow poll plus scheduling, not the 30ms interval.
        now_ms = time.time() * 1000 if now_ms is None else now_ms
        self.assertLess(now_ms - self.beats[-1], 1000)

    def git(self, args, cwd):
        return 'origin\n' if args == ['git', 'remote'] else ''

    def events(self):
        return [json.loads(row[0]) for row in self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ?"
            " AND kind = 'merge_lock_wait' ORDER BY seq", (self.waiter,))]

    def fetched(self, args, **kwargs):
        self.assertEqual(gates.read_merge_lock(self.path)[0], self.waiter)
        self.assertEqual([event['state'] for event in self.events()],
                         ['begin', 'end'])
        # No remote main yet: the successful fetch suffices to cut locally.
        return SimpleNamespace(returncode=0 if args[1] == 'fetch' else 1)

    def cut(self):
        return claim._cut_worktree(self.tgt, self.conn, self.waiter, None,
                                   'KO-2', 'task', 'task/test', self.root / 'wt')

    def test_live_holder_waits_past_default_with_heartbeat_and_event(self):
        self.release_from = 400
        self.assertTrue(self.cut())
        self.assertEqual(set(self.phases), {'working'})
        self.assertEqual(store.run_phase(self.conn, self.waiter), 'working')
        # No setup commands repair the phase before implementation finishes.
        store.set_phase(self.conn, self.waiter, 'verifying')
        self.assertEqual(store.run_phase(self.conn, self.waiter), 'verifying')
        self.assertGreaterEqual(self.elapsed(), 400)
        self.assert_waiter_beat()
        self.assertEqual(self.fetch.call_args_list[0].args[0],
                         ['git', 'fetch', 'origin'])
        begin, end = self.events()
        self.assertEqual((begin['holder'], end['holder']),
                         (self.holder, self.holder))
        self.assertGreaterEqual(end['waited'], 180)
        self.assertFalse(self.path.exists())

    def test_ended_holder_fails_at_default_naming_fetch(self):
        self.live = False
        store.release(self.conn, self.holder, 'failed', 'ended')
        with self.assertRaisesRegex(
                gates.MergeLockHeld, 'the fetch before the cut did not run'):
            self.cut()
        self.assertGreaterEqual(self.elapsed(), 180)
        self.assertLess(self.elapsed(), self.within(180))
        self.assertEqual(self.events(), [])
        self.fetch.assert_not_called()
        self.assertEqual(gates.read_merge_lock(self.path)[0], self.holder)

    def test_live_holder_ceiling_names_holder_and_fetch(self):
        with self.assertRaisesRegex(
                gates.MergeLockHeld,
                f'run {self.holder}.*the fetch before the cut did not run'):
            self.cut()
        self.assertGreaterEqual(self.elapsed(), 600)
        self.assertEqual(store.run_phase(self.conn, self.waiter), 'working')
        self.assertLess(self.elapsed(), self.within(600))
        self.assertEqual([event['state'] for event in self.events()],
                         ['begin', 'end'])
        self.fetch.assert_not_called()
        self.assertEqual(gates.read_merge_lock(self.path)[0], self.holder)

    def test_heartbeat_check_allows_a_slow_poll(self):
        now = time.time() * 1000
        self.beats = [now - 1500, now - 1200, now - 900]
        self.assert_waiter_beat(now)

    def test_heartbeat_check_fails_a_silent_waiter(self):
        self.enterContext(patch.object(
            merge_lock, 'heartbeat_while',
            lambda conn, run_id, interval_s: contextlib.nullcontext()))
        self.release_from = 400
        self.assertTrue(self.cut())
        with self.assertRaises(AssertionError):
            self.assert_waiter_beat()


class LoadedRunnerClaimLockWaitTests(ClaimLockWaitTests):
    """Each poll oversleeps by 150ms of real time; lock time moves with it."""
    delay_ms = 150


class BusyRunnerClaimLockWaitTests(ClaimLockWaitTests):
    """Each poll oversleeps by 400ms: one poll past the default, then the
    ceiling, and the live holder is still waited for."""
    delay_ms = 400
