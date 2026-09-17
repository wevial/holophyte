"""The checkout is updated before the restart note and process replacement."""
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from holophyte import operator
from tests.loop_fixture import LoopFixture


class ReexecTests(LoopFixture):
    def restart(self, branch='main', dirty='', failure=None):
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
                return dirty
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
        for kwargs, reason in (({'branch': 'topic'}, 'not on main'),
                               ({'dirty': ' M tracked'}, 'not clean')):
            with self.subTest(reason=reason):
                events, output = self.restart(**kwargs)
                self.assertNotIn(['git', 'merge', '--ff-only', 'origin/main'], events)
                self.assertNotIn(['git', 'fetch', 'origin', 'main'], events)
                self.assertEqual(output.count('checkout not fast-forwarded'), 1)
                self.assertIn(reason, output)
                self.assertEqual(events[-1], 'EXEC')

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
