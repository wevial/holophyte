"""A healthy gate holder earns a bounded wait, without losing exclusivity."""
import contextlib
import json
from unittest.mock import patch

import holophyte.gates as gates
import holophyte.merge_gate as gate
import store
from tests.sweep_fixture import T0, SweepTestCase


class LiveMergeLockTests(SweepTestCase):
    def setUp(self):
        super().setUp()
        self.configure('[merge]\ncheck_wait_sec = 300\n')
        self.holder = self.a_run(phase="merge_gate")
        self.waiter = self.a_run(phase="reviewing")
        self.path = gates.merge_lock_path(self.tgt)
        self.path.write_text(f"{self.holder} {T0 / 1000}\n")
        self.elapsed = 0
        self.release_at = None
        self.die_at = None
        self.stale_at = None
        self.live = True
        self.stack = self.enterContext(contextlib.ExitStack())
        for name in ('holophyte.gates.monotonic',
                     'holophyte.merge_lock.monotonic'):
            self.stack.enter_context(patch(name, lambda: self.elapsed))
        self.stack.enter_context(patch('holophyte.merge_lock.time',
                                      lambda: T0 / 1000 + self.elapsed))
        self.stack.enter_context(patch('holophyte.gates.sleep', self.sleep))
        self.park = self.stack.enter_context(patch.object(gate, '_park_at_gate'))

    def sleep(self, seconds):
        self.elapsed += seconds
        if self.live:
            store.heartbeat(self.conn, self.holder,
                            now=T0 + int(self.elapsed * 1000))
        if self.die_at and self.elapsed >= self.die_at:
            store.release(self.conn, self.holder, 'failed', 'holder ended')
            self.die_at = None
            self.live = False
        if self.stale_at and self.elapsed >= self.stale_at:
            store.heartbeat(self.conn, self.holder, now=T0 - 1_000_000)
            self.live = False
        if self.release_at and self.elapsed >= self.release_at:
            self.path.unlink()
            self.release_at = None
        if self.elapsed > 180:
            self.assertEqual(store.run_phase(self.conn, self.waiter), 'merge_gate')
            self.assertEqual(self.events()[-1]['state'], 'begin')
        holder = gates.read_merge_lock(self.path)
        if holder:
            self.assertEqual(holder[0], self.holder)

    def acquire(self):
        return gate._gate_lock(self.tgt, self.conn, self.waiter, None,
                               'KO-2', 'task/test', 'abc', 60)

    def events(self):
        return [json.loads(row[0]) for row in self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ?"
            " AND kind = 'merge_lock_wait' ORDER BY seq", (self.waiter,))]

    def test_live_holder_releases_after_400_seconds(self):
        self.release_at = 400
        with self.acquire():
            self.assertEqual(gates.read_merge_lock(self.path)[0], self.waiter)
            self.assertGreaterEqual(self.elapsed, 400)
            self.assertLessEqual(self.elapsed, 430)
            begin, end = self.events()
            self.assertEqual((begin['state'], end['state']), ('begin', 'end'))
            self.assertEqual((begin['holder'], end['holder']),
                             (self.holder, self.holder))
            self.assertEqual(begin['since'], T0 / 1000)
            self.assertEqual(end['waited'], self.elapsed)
        self.assertFalse(self.path.exists())
        self.park.assert_not_called()

    def test_ended_or_stale_holder_keeps_180_second_timeout(self):
        for ended in (False, True):
            with self.subTest(ended=ended):
                self.elapsed = 0
                self.live = False
                store.heartbeat(self.conn, self.holder, now=T0 - 1_000_000)
                if ended:
                    store.release(self.conn, self.holder, 'failed', 'ended')
                with self.assertRaisesRegex(gates.MergeLockHeld,
                                            r'180s wait.*--sweep --act'):
                    with self.acquire():
                        self.fail('entered a held lock')
                self.assertEqual(self.elapsed, 180)
                self.assertEqual(self.events(), [])
                self.assertTrue(self.path.exists())

    def test_ceiling_and_holder_ending_during_extended_wait(self):
        for dies, expected in ((None, 600), (240, 240)):
            with self.subTest(dies=dies):
                self.elapsed = 0
                self.die_at = dies
                with self.assertRaisesRegex(gates.MergeLockHeld,
                                            f'run {self.holder}.*waited {expected}s'):
                    with self.acquire():
                        self.fail('entered a held lock')
                self.assertEqual(self.elapsed, expected)
                self.assertEqual(self.events()[-1]['waited'], expected)
                self.assertEqual(self.events()[-1]['state'], 'end')
                self.assertTrue(self.path.exists())

    def test_holder_becoming_stale_during_extended_wait(self):
        self.stale_at = 240
        with self.assertRaisesRegex(gates.MergeLockHeld, 'waited 240s'):
            with self.acquire():
                self.fail('entered a stale holder lock')
        self.assertEqual(self.elapsed, 240)
        self.assertEqual([event['state'] for event in self.events()],
                         ['begin', 'end'])
        self.park.assert_called_once()
