"""Attribution enforcement at the publication boundary, using real Git."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from holophyte import pr

ATTRIBUTION = ('\n\nGenerated with [Devin](<https://devin.ai>)\n\n'
               'Co-Authored-By: Devin <devin-ai-integration@users.noreply.github.com>')


class CommitHygieneTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.remote = self.root / 'remote.git'
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test Writer')
        self.git('config', 'user.email', 'writer@example.com')
        self.commit('base')
        self.git('init', '--bare', str(self.remote))
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', 'origin', 'main')
        self.git('checkout', '-b', 'task')
        self.config = {}
        self.target = SimpleNamespace(path=self.repo, config=lambda: self.config,
                                      config_path=self.root / 'config.toml')

    def git(self, *args, cwd=None):
        return subprocess.check_output(['git', *args], cwd=cwd or self.repo,
                                       stderr=subprocess.PIPE, text=True).strip()

    def commit(self, message):
        (self.repo / 'content').write_text(message)
        self.git('add', 'content')
        self.git('commit', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def test_push_cleans_only_new_attribution_and_preserves_metadata(self):
        first = self.commit('First change')
        second = self.commit('Second change\n\nReal details.' + ATTRIBUTION)
        metadata = self.git('show', '-s', '--format=%T%n%an%n%ae%n%aI%n%cI', second)
        pr.push_branch(self.target, 'task')
        tip = self.git('rev-parse', 'task', cwd=self.remote)
        self.assertNotEqual(tip, second)
        self.assertEqual(self.git('show', '-s', '--format=%B', tip),
                         'Second change\n\nReal details.')
        raw = subprocess.check_output(['git', 'cat-file', 'commit', tip],
                                      cwd=self.remote)
        self.assertEqual(raw.split(b'\n\n', 1)[1],
                         b'Second change\n\nReal details.\n')
        self.assertEqual(self.git('rev-parse', tip + '^'), first)
        self.assertEqual(self.git('show', '-s', '--format=%T%n%an%n%ae%n%aI%n%cI', tip),
                         metadata)

    def test_published_attribution_and_human_trailer_are_untouched(self):
        published = self.commit('Published' + ATTRIBUTION)
        self.git('push', 'origin', 'task')
        # Even a missing/stale tracking ref cannot license rewriting remote history.
        self.git('update-ref', '-d', 'refs/remotes/origin/task')
        message = 'Next change\n\nCo-Authored-By: Alice Example <alice@example.com>'
        tip = self.commit(message)
        pr.push_branch(self.target, 'task')
        self.assertEqual(self.git('rev-parse', 'task', cwd=self.remote), tip)
        self.assertEqual(self.git('rev-parse', tip + '^'), published)
        self.assertEqual(self.git('show', '-s', '--format=%B', tip), message)

    def test_push_prunes_conflicting_stale_tracking_refs_before_cleanup(self):
        base = self.git('rev-parse', 'main')
        self.git('push', 'origin', 'main:refs/heads/foo')
        self.git('fetch', 'origin')
        # Simulate another clone replacing foo without updating our tracking ref.
        self.git('update-ref', '-d', 'refs/heads/foo', cwd=self.remote)
        self.git('update-ref', 'refs/heads/foo/bar', base, cwd=self.remote)
        self.assertEqual(self.git('rev-parse', 'refs/remotes/origin/foo'), base)
        original = self.commit('New change' + ATTRIBUTION)
        tree = self.git('rev-parse', 'HEAD^{tree}')

        pr.push_branch(self.target, 'task')

        tip = self.git('rev-parse', 'task', cwd=self.remote)
        self.assertNotEqual(tip, original)
        self.assertEqual(self.git('show', '-s', '--format=%B', tip), 'New change')
        self.assertEqual(self.git('rev-parse', tip + '^{tree}'), tree)
        self.assertEqual(self.git('rev-parse', 'refs/remotes/origin/foo/bar'), base)
        self.assertEqual(self.git('for-each-ref', '--format=%(refname)',
                                  'refs/remotes/origin/foo'),
                         'refs/remotes/origin/foo/bar')

    def test_disabled_and_custom_patterns(self):
        self.config = {'merge': {'strip_attribution': []}}
        tip = self.commit('Keep this' + ATTRIBUTION)
        pr.push_branch(self.target, 'task')
        self.assertEqual(self.git('rev-parse', 'task', cwd=self.remote), tip)
        self.config['merge']['strip_attribution'] = ['^Build credit:']
        self.commit('Custom\n\nBuild credit: automation')
        pr.push_branch(self.target, 'task')
        self.assertEqual(self.git('show', '-s', '--format=%B', 'task'), 'Custom')

    def test_tree_mismatch_aborts_without_moving_branch_or_worktree(self):
        from holophyte import commit_hygiene
        from holophyte.gates import InfraFailure
        tip = self.commit('Change' + ATTRIBUTION)
        (self.repo / 'content').write_text('unstaged work')
        self.git('add', 'content')
        (self.repo / 'content').write_text('more unstaged work')
        before = self.git('diff', '--binary', 'HEAD')
        actual = commit_hygiene._git

        def mismatched(wt, *args, **kwargs):
            if args == ('rev-parse', tip + '^{tree}'):
                return b'wrong-tree\n'
            return actual(wt, *args, **kwargs)

        with patch.object(commit_hygiene, '_git', side_effect=mismatched):
            with self.assertRaisesRegex(InfraFailure, 'tree'):
                pr.push_branch(self.target, 'task')
        self.assertEqual(self.git('rev-parse', 'task'), tip)
        self.assertEqual(self.git('diff', '--binary', 'HEAD'), before)
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/task'), '')

    def test_local_merge_cleans_attribution_without_a_remote(self):
        from holophyte.merge_gate import _merge
        self.git('remote', 'remove', 'origin')
        original = self.commit('Local change\n\n🤖 Generated with Claude Code\n\n'
                               'Co-Authored-By: Claude <noreply@anthropic.com>')
        tree = self.git('rev-parse', 'HEAD^{tree}')
        self.git('checkout', 'main')
        wt = self.root / 'task-worktree'
        self.git('worktree', 'add', str(wt), 'task')
        with patch('holophyte.merge_gate.commit_findings'), \
                patch('holophyte.merge_gate.set_phase'):
            _merge(self.target, None, None, None, 'KO-560', 'Local task',
                   'task', wt, original)
        self.assertEqual(self.git('show', '-s', '--format=%B', 'main^2'),
                         'Local change')
        self.assertEqual(self.git('rev-parse', 'main^{tree}'), tree)

    def test_reword_preserves_merge_graph_and_dirty_index_without_hooks(self):
        from holophyte.commit_hygiene import strip_attribution
        self.git('checkout', '-b', 'side')
        side = self.commit('Side' + ATTRIBUTION)
        self.git('checkout', 'task')
        (self.repo / 'other').write_text('independent change')
        self.git('add', 'other')
        self.git('commit', '-m', 'Task change')
        parent = self.git('rev-parse', 'HEAD')
        self.git('merge', '--no-ff', 'side', '-m', 'Merge side')
        tree = self.git('rev-parse', 'HEAD^{tree}')
        hooks = self.root / 'hooks'
        hooks.mkdir()
        hook = hooks / 'reference-transaction'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o755)
        self.git('config', 'core.hooksPath', str(hooks))
        (self.repo / 'other').write_text('staged')
        self.git('add', 'other')
        (self.repo / 'other').write_text('unstaged')
        staged = self.git('diff', '--cached')
        dirty = self.git('diff')
        strip_attribution(self.target, self.repo, 'task')
        self.assertEqual(self.git('rev-parse', 'HEAD^{tree}'), tree)
        self.assertEqual(self.git('rev-parse', 'HEAD^1'), parent)
        self.assertNotEqual(self.git('rev-parse', 'HEAD^2'), side)
        self.assertEqual(self.git('show', '-s', '--format=%B', 'HEAD^2'), 'Side')
        self.assertEqual(self.git('diff', '--cached'), staged)
        self.assertEqual(self.git('diff'), dirty)
