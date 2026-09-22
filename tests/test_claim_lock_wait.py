"""Claim-time fetch contention, with accelerated lock time and real heartbeats."""
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import holophyte.claim as claim
import holophyte.gates as gates
import store
import store.read
from tests.sweep_fixture import SweepTestCase


class ClaimLockWaitTests(SweepTestCase):
    def setUp(self):
        super().setUp()
        self.configure('[merge]\ncheck_wait_sec = 300\n'
                       '[supervisor]\nheartbeat_stale_min = 0.001\n')
        self.holder = self.a_run(phase='merge_gate')
        self.waiter = self.a_run()
        self.path = gates.merge_lock_path(self.tgt)
        self.path.write_text(f'{self.holder} {time.time()}\n')
        self.release_at = None
        self.live = True
        self.beats = []
        self.started = time.monotonic()
        # 180 lock seconds take 180ms; heartbeat timers keep real time.
        for name in ('holophyte.gates.monotonic',
                     'holophyte.merge_lock.monotonic'):
            self.enterContext(patch(name, self.elapsed))
        self.enterContext(patch('holophyte.gates.MERGE_LOCK_POLL_SEC', 10))
        self.enterContext(patch('holophyte.merge_lock.CHECK_POLL_S', 10))
        self.enterContext(patch('holophyte.gates.sleep', self.poll))
        self.enterContext(patch.object(claim, 'sh', self.git))
        self.fetch = self.enterContext(patch.object(
            claim.subprocess, 'run', side_effect=self.fetched))

    def elapsed(self):
        return (time.monotonic() - self.started) * 1000

    def poll(self, seconds):
        if self.live:
            store.heartbeat(self.conn, self.holder)
        time.sleep(seconds / 1000)
        self.beats.append(store.read.run_snapshot(
            self.conn, self.waiter).lastHeartbeat)
        if self.release_at and self.elapsed() >= self.release_at:
            self.path.unlink()
            self.release_at = None

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
        self.release_at = 400
        self.assertTrue(self.cut())
        self.assertGreaterEqual(self.elapsed(), 400)
        self.assertGreater(len(set(self.beats)), 3)
        self.assertLess(time.time() * 1000 - self.beats[-1], 100)
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
        self.assertLess(self.elapsed(), 350)
        self.assertEqual(self.events(), [])
        self.fetch.assert_not_called()
        self.assertEqual(gates.read_merge_lock(self.path)[0], self.holder)

    def test_live_holder_ceiling_names_holder_and_fetch(self):
        with self.assertRaisesRegex(
                gates.MergeLockHeld,
                f'run {self.holder}.*the fetch before the cut did not run'):
            self.cut()
        self.assertGreaterEqual(self.elapsed(), 600)
        self.assertLess(self.elapsed(), 850)
        self.assertEqual([event['state'] for event in self.events()],
                         ['begin', 'end'])
        self.fetch.assert_not_called()
        self.assertEqual(gates.read_merge_lock(self.path)[0], self.holder)
