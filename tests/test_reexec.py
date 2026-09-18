"""The checkout is updated before the restart note and process replacement."""
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from holophyte import operator
from store.schema import SCHEMA_VERSION
from tests.loop_fixture import LoopFixture


class ReexecTests(LoopFixture):
    def restart(self, branch='main', dirty='', failure=None, untracked='',
                workers=None):
        events = []
        head = ['old1234']
        conn = Mock()
        out = io.StringIO()

        def sh(args, cwd):
            self.assertEqual(cwd, self.tgt.path)
            events.append(args)
            if args == ['git', 'rev-parse', '--short', 'HEAD']:
                return head[0]
            if args == ['git', 'show', 'origin/main:store/schema.py']:
                return f'SCHEMA_VERSION = {SCHEMA_VERSION}\n'
            if args == ['git', 'rev-parse', '--short', 'origin/main']:
                return 'new5678'
            if args == ['git', 'branch', '--show-current']:
                return branch
            if args == ['git', 'status', '--porcelain']:
                return '\n'.join(filter(None, (dirty, untracked))).strip()
            if args == ['git', 'status', '--porcelain', '--untracked-files=no']:
                return dirty.strip()
            if failure and args == failure:
                raise RuntimeError('git failed: refused\nsecond line')
            if args == ['git', 'merge', '--ff-only', 'origin/main']:
                head[0] = 'new5678'
            return ''

        def note(*args):
            events.append(('note', json.loads(args[2])))

        with patch.object(operator, 'sh', sh), \
                patch.object(operator.store, 'record_loop_restart', note), \
                patch.object(operator, 'EXEC', lambda *a: events.append('EXEC')), \
                redirect_stdout(out):
            operator._reexec(self.tgt, conn, 1, worker_pids=workers or {})
        return events, out.getvalue()

    def test_unchanged_schema_fast_forwards_under_two_live_workers(self):
        workers = {5001: Mock(), 5002: Mock()}
        with patch("os.kill") as signal_worker, \
                patch("holophyte.pool.WAIT") as wait:
            events, output = self.restart(workers=workers)
        signal_worker.assert_not_called()
        wait.assert_not_called()
        self.assertLess(events.index(['git', 'fetch', 'origin', 'main']),
                        events.index(['git', 'show', 'origin/main:store/schema.py']))
        self.assertIn(['git', 'merge', '--ff-only', 'origin/main'], events)
        self.assertEqual(events[-1], 'EXEC')
        self.assertIn(f'schema unchanged ({SCHEMA_VERSION}); fast-forwarding'
                      ' to new5678 under 2 live worker(s)', output)
        for worker in workers.values():
            self.assertEqual(worker.mock_calls, [])

    def test_fast_forward_precedes_note_and_exec(self):
        events, output = self.restart()
        fetch = events.index(['git', 'fetch', 'origin', 'main'])
        merge = events.index(['git', 'merge', '--ff-only', 'origin/main'])
        self.assertLess(fetch, merge)
        self.assertLess(merge, len(events) - 2)
        self.assertEqual(events[-2:], [
            ('note', {'leaving': 'old1234', 'arriving': 'new5678'}), 'EXEC'])
        self.assertIn('re-executing at new5678', output)

    def test_unsafe_checkouts_refuse_once_and_still_exec(self):
        for kwargs, reason in (
                ({'branch': 'topic'}, 'not on main'),
                ({'dirty': ' M tracked'}, 'checkout not clean: tracked')):
            with self.subTest(reason=reason):
                events, output = self.restart(**kwargs)
                self.assertNotIn(['git', 'merge', '--ff-only', 'origin/main'], events)
                self.assertIn(['git', 'fetch', 'origin', 'main'], events)
                self.assertEqual(output.count('checkout not fast-forwarded'), 1)
                self.assertIn(reason, output)
                self.assertEqual(events[-1], 'EXEC')

    def test_untracked_files_allow_fast_forward(self):
        events, output = self.restart(untracked='?? .env.bak-2026-09-03')
        self.assertIn(['git', 'status', '--porcelain', '--untracked-files=no'], events)
        self.assertIn(['git', 'fetch', 'origin', 'main'], events)
        self.assertIn(['git', 'merge', '--ff-only', 'origin/main'], events)
        self.assertNotIn('checkout not fast-forwarded', output)

    def test_dirty_refusal_names_at_most_three_paths(self):
        events, output = self.restart(dirty=' M first\n M second\n M third\n M fourth')
        self.assertIn('(checkout not clean: first, second, third)', output)
        self.assertNotIn('fourth', output)
        self.assertNotIn(['git', 'merge', '--ff-only', 'origin/main'], events)

    def test_git_failure_still_executes_disk_build(self):
        for command in (['git', 'fetch', 'origin', 'main'],
                        ['git', 'merge', '--ff-only', 'origin/main']):
            with self.subTest(command=command):
                events, output = self.restart(failure=command)
                self.assertEqual(events[-2:], [
                    ('note', {'leaving': 'old1234', 'arriving': 'old1234'}), 'EXEC'])
                self.assertEqual(output.count('checkout not fast-forwarded'), 1)


if __name__ == '__main__':
    unittest.main()
