"""The checkout is updated before the restart note and process replacement."""
import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from holophyte import operator, pool_handoff
from store.schema import SCHEMA_VERSION
from tests.loop_fixture import LoopFixture


class ReexecTests(LoopFixture):
    def test_restarted_loop_waits_for_supervisor_stamp(self):
        import store
        from holophyte.reexec import wait_for_supervisor

        conn = store.open(self.db)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
        conn.close()
        waits = []

        def stamp(seconds):
            waits.append(seconds)
            # The owner is the only actor allowed to advance the version.
            store.open(self.db, migrate="owner").close()

        out = io.StringIO()
        with redirect_stdout(out):
            wait_for_supervisor(self.tgt, wait=stamp)
        self.assertEqual(waits, [1])
        self.assertIn("waiting for the supervisor", out.getvalue())
        store.open(self.db).close()

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
                self.assertIn(f"factory checkout not fast-forwarded at {self.target}",
                              output)
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


class SeparateCheckoutTests(LoopFixture):
    def setUp(self):
        super().setUp()
        self.factory = self.target.parent / "factory"
        self.git("init", "-q", "-b", "main", str(self.factory))
        self.git("config", "user.email", "factory@example.invalid", cwd=self.factory)
        self.git("config", "user.name", "Factory Test", cwd=self.factory)
        schema = self.factory / "store" / "schema.py"
        schema.parent.mkdir()
        schema.write_text(f"SCHEMA_VERSION = {SCHEMA_VERSION}\n")
        self.git("add", ".", cwd=self.factory)
        self.git("commit", "-qm", "factory base", cwd=self.factory)
        self.leaving = self.git("rev-parse", "--short", "HEAD",
                                cwd=self.factory).strip()
        for repo in (self.factory, self.target):
            remote = repo.with_name(repo.name + "-origin")
            self.git("clone", "-q", str(repo), str(remote))
            self.git("config", "user.email", "factory@example.invalid", cwd=remote)
            self.git("config", "user.name", "Factory Test", cwd=remote)
            path = remote / ("store/schema.py" if repo == self.factory else "README.md")
            path.write_text(f"SCHEMA_VERSION = {SCHEMA_VERSION + 1}\n"
                            if repo == self.factory else "target advanced\n")
            self.git("add", ".", cwd=remote)
            self.git("commit", "-qm", "remote advance", cwd=remote)
            self.git("remote", "add", "origin", str(remote), cwd=repo)
        self.arriving = self.git("rev-parse", "--short", "HEAD",
                                 cwd=self.factory.with_name("factory-origin")).strip()
        patcher = patch.object(pool_handoff, "factory_checkout",
                               return_value=self.factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_schema_gate_reads_factory_remote(self):
        with redirect_stdout(io.StringIO()):
            can_ff, changed = pool_handoff._prepare_reexec(self.tgt, {})
        self.assertTrue(can_ff)
        self.assertTrue(changed)
        self.assertEqual(pool_handoff.fetched_schema(self.tgt),
                         (SCHEMA_VERSION + 1, None))

    def test_restart_moves_only_factory_and_reports_its_shas(self):
        for dirty in (False, True):
            with self.subTest(dirty_target=dirty):
                self.git("reset", "--hard", self.leaving, cwd=self.factory)
                if dirty:
                    (self.target / "README.md").write_text("uncommitted target work\n")
                state = SimpleNamespace(restart=True, stopped=False,
                                        restart_reason=None, prepared_sha=None)
                out = io.StringIO()
                with redirect_stdout(out), patch.object(operator, "EXEC"), \
                        patch.object(operator.store, "record_loop_restart") as note:
                    self.assertFalse(pool_handoff.prepare_restart(state, self.tgt, {}))
                    self.assertTrue(state.can_ff)
                    operator._reexec(self.tgt, Mock(), 1,
                                     prepared_sha=state.prepared_sha,
                                     can_ff=state.can_ff)
                self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
                self.assertEqual(self.git("rev-parse", "--short", "HEAD",
                                          cwd=self.factory).strip(), self.arriving)
                self.assertIn(f"fast-forward to {self.arriving} "
                              f"(leaving {self.leaving})",
                              out.getvalue())
                self.assertNotIn("checkout not fast-forwarded", out.getvalue())
                self.assertEqual(json.loads(note.call_args.args[2]),
                                 {"leaving": self.leaving, "arriving": self.arriving})
                self.assertFalse((self.target / ".git" / "FETCH_HEAD").exists())
                if dirty:
                    self.assertEqual((self.target / "README.md").read_text(),
                                     "uncommitted target work\n")


if __name__ == '__main__':
    unittest.main()
