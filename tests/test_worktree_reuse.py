"""Leftover-worktree reuse readies the tree without destroying anything.

`reuse_leftover()` is the arm of `run_task()` that runs when a previous
failed run left its worktree behind. Its contract — preserved work survives —
is exercised directly here against a real repo: the two states git refuses to
plough through (an unregistered directory, a dirty tree) must come back as a
clean refusal or a WIP commit, never a RuntimeError, and nothing under the
leftover may be deleted on any path.

Run: python3 -m unittest discover -s tests -p 'test_worktree_reuse*' -v
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
SPEC = importlib.util.spec_from_file_location("holophyte_factory", ROOT / "factory.py")
factory = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(factory)


class ReuseFixture(unittest.TestCase):
    """A real target repo and worktree directory, patched into the factory."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.target = root / "repo"
        self.worktrees = root / "repo.worktrees"
        self.target.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "factory@example.invalid")
        self.git("config", "user.name", "Factory Test")
        (self.target / "README.md").write_text("base\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "main").strip()
        for name, value in (("TARGET", self.target),
                            ("WORKTREES", self.worktrees)):
            patcher = patch.object(factory, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.wt = self.worktrees / "add-a-thing"
        self.branch = "task/add-a-thing"

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.target),
                              check=True, capture_output=True, text=True).stdout

    def leftover_worktree(self):
        """A registered leftover on `self.branch`, as a failed run leaves it."""
        self.git("worktree", "add", "--detach", str(self.wt), "main")
        self.git("checkout", "-b", self.branch, cwd=self.wt)


class UnregisteredLeftoverTests(ReuseFixture):
    def test_an_unregistered_directory_is_refused_and_left_intact(self):
        """A directory git does not know about cannot be reused (`worktree
        add` dies on it) and must not be deleted (it may hold rescued work):
        the answer is a refusal naming the directory, with its contents
        untouched."""
        self.wt.mkdir(parents=True)
        (self.wt / "precious.txt").write_text("rescued work\n")

        ok, why = factory.reuse_leftover(self.wt, self.branch)

        self.assertFalse(ok)
        self.assertIn(str(self.wt), why)
        self.assertIn("not a registered worktree", why)
        self.assertEqual((self.wt / "precious.txt").read_text(),
                         "rescued work\n")


class DirtyLeftoverTests(ReuseFixture):
    def test_uncommitted_changes_become_a_wip_commit_on_the_branch(self):
        """A dirty leftover is exactly the state with something to preserve
        (run 9 of the KO-146 incident died here on `checkout -B`): the
        changes land as a WIP commit on the branch and reuse proceeds."""
        self.leftover_worktree()
        (self.wt / "notes.txt").write_text("uncommitted rescue\n")

        ok, why = factory.reuse_leftover(self.wt, self.branch)

        self.assertTrue(ok, why)
        self.assertEqual(self.git("status", "--porcelain", cwd=self.wt), "")
        self.assertIn("WIP", self.git("log", "-1", "--format=%s", cwd=self.wt))
        self.assertEqual(
            self.git("show", "HEAD:notes.txt", cwd=self.wt),
            "uncommitted rescue\n")

    def test_a_clean_registered_leftover_is_reused_on_its_branch(self):
        self.leftover_worktree()

        ok, why = factory.reuse_leftover(self.wt, self.branch)

        self.assertTrue(ok, why)
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.wt).strip(),
            self.branch)


if __name__ == "__main__":
    unittest.main()
