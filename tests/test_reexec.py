"""The checkout is updated before the restart note and process replacement."""
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from holophyte import operator, startup
from tests.loop_fixture import LoopFixture


class ReexecTests(LoopFixture):
    def restart(self, branch='main', dirty='', failure=None, untracked=''):
        events = []
        head = ['old1234']
        conn = Mock()
        out = io.StringIO()

        def sh(args, cwd):
            self.assertEqual(cwd, self.tgt.path)
            events.append(args)
            if args == ['git', 'rev-parse', '--short', 'HEAD']:
                return head[0]
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
            operator._reexec(self.tgt, conn, 1)
        return events, out.getvalue()

    def test_live_pool_worker_blocks_checkout_move(self):
        from holophyte import pool_handoff
        from holophyte.pool import _PoolState

        state = _PoolState(True, True)
        state.restart = True
        with subprocess.Popen([sys.executable, '-c',
                               'import sys; sys.stdin.read()'],
                              stdin=subprocess.PIPE) as child:
            try:
                pool = {child.pid: (1, child)}
                out = io.StringIO()
                with patch.object(operator, 'sh', return_value='old1234') as git, \
                        patch.object(startup, 'build_sha', return_value='start42'), \
                        redirect_stdout(out):
                    pool_handoff.prepare_restart(state, self.tgt, pool)
                self.assertFalse(any(call.args[0][:2] == ['git', 'merge']
                                     for call in git.call_args_list))
                self.assertIn('checkout not fast-forwarded: 1 worker(s) still on'
                              ' start42', out.getvalue())
            finally:
                child.stdin.close()
                child.wait(timeout=10)

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
                self.assertNotIn(['git', 'fetch', 'origin', 'main'], events)
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
