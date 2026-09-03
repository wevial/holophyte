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

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
import holophyte.loop  # noqa: E402 - after the sys.path insert above
import holophyte.target  # noqa: E402 - after the sys.path insert above


class ReuseFixture(unittest.TestCase):
    """A real target repo and worktree directory, as a `Target`."""

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
        # The test the scripted approvals name as their witness: since KO-215
        # the loop checks a named test exists in the worktree.
        (self.target / "tests").mkdir()
        (self.target / "tests" / "test_thing.py").write_text(
            "def test_it_works():\n    pass\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "main").strip()
        self.tgt = holophyte.target.Target(
            path=self.target, holo_dir=root, store_path=root / "store.db",
            config_path=root / "config.toml", worktrees=self.worktrees)
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

        ok, why = holophyte.loop.reuse_leftover(self.tgt, self.wt, self.branch)

        self.assertFalse(ok)
        self.assertIn(str(self.wt), why)
        self.assertIn("not a registered worktree", why)
        self.assertEqual((self.wt / "precious.txt").read_text(),
                         "rescued work\n")

    def test_a_registered_prefix_sibling_does_not_vouch_for_the_directory(self):
        """Slugs are truncated titles, so `add-a-thing` and
        `add-a-thing-later` coexist; a substring test over `worktree list`
        would read the registered sibling as covering the unregistered
        directory and then crash on `git status` inside it."""
        self.git("worktree", "add", "--detach",
                 str(self.worktrees / "add-a-thing-later"), "main")
        self.wt.mkdir(parents=True)
        (self.wt / "precious.txt").write_text("rescued work\n")

        ok, why = holophyte.loop.reuse_leftover(self.tgt, self.wt, self.branch)

        self.assertFalse(ok)
        self.assertIn("not a registered worktree", why)
        self.assertEqual((self.wt / "precious.txt").read_text(),
                         "rescued work\n")

    def test_a_worktree_reached_through_a_symlink_is_recognized(self):
        """`git worktree list` prints resolved paths; a target configured
        through a symlink must not have every healthy leftover refused."""
        self.leftover_worktree()
        alias = self.worktrees.parent / "alias"
        alias.symlink_to(self.worktrees)

        ok, why = holophyte.loop.reuse_leftover(self.tgt, alias / "add-a-thing",
                                                self.branch)

        self.assertTrue(ok, why)


class DirtyLeftoverTests(ReuseFixture):
    def test_uncommitted_changes_become_a_wip_commit_on_the_branch(self):
        """A dirty leftover is exactly the state with something to preserve
        (run 9 of the KO-146 incident died here on `checkout -B`): the
        changes land as a WIP commit on the branch and reuse proceeds."""
        self.leftover_worktree()
        (self.wt / "notes.txt").write_text("uncommitted rescue\n")

        ok, why = holophyte.loop.reuse_leftover(self.tgt, self.wt, self.branch)

        self.assertTrue(ok, why)
        self.assertEqual(self.git("status", "--porcelain", cwd=self.wt), "")
        self.assertIn("WIP", self.git("log", "-1", "--format=%s", cwd=self.wt))
        self.assertEqual(
            self.git("show", "HEAD:notes.txt", cwd=self.wt),
            "uncommitted rescue\n")

    def test_a_clean_registered_leftover_is_reused_on_its_branch(self):
        self.leftover_worktree()

        ok, why = holophyte.loop.reuse_leftover(self.tgt, self.wt, self.branch)

        self.assertTrue(ok, why)
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.wt).strip(),
            self.branch)


class CarriedCommitsTests(ReuseFixture):
    def test_preserved_commits_stay_on_the_branch_tip(self):
        """A leftover holding commits main does not have is exactly the state
        reuse exists to protect (KO-146: the unconditional reset to main is
        what orphaned the rescue commits and made run 10 read as 'implementer
        made no commits')."""
        self.leftover_worktree()
        (self.wt / "work.txt").write_text("preserved\n")
        self.git("add", "-A", cwd=self.wt)
        self.git("commit", "-q", "-m", "rescued: preserved work", cwd=self.wt)
        tip = self.git("rev-parse", "HEAD", cwd=self.wt).strip()

        ok, why = holophyte.loop.reuse_leftover(self.tgt, self.wt, self.branch)

        self.assertTrue(ok, why)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.wt).strip(),
                         tip)

    def test_a_verifiably_empty_leftover_is_reset_to_main(self):
        """Clean tree, nothing main does not already have: the one case where
        resetting loses no work — and what keeps a reused run from starting
        behind a main that moved on since the leftover was cut."""
        self.leftover_worktree()
        (self.target / "new.txt").write_text("newer main\n")
        self.git("add", "new.txt")
        self.git("commit", "-q", "-m", "main moved on")

        ok, why = holophyte.loop.reuse_leftover(self.tgt, self.wt, self.branch)

        self.assertTrue(ok, why)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.wt).strip(),
                         self.git("rev-parse", "main").strip())

    def test_a_carried_branch_under_a_moved_on_main_absorbs_main(self):
        """The review routes and the merge require main to be an ancestor of
        the candidate; a carried branch that predates the current main would
        raise out of every review dispatch and stall the ticket on each
        rerun. Reuse merges main in, keeping the preserved commits."""
        self.leftover_worktree()
        (self.wt / "work.txt").write_text("preserved\n")
        self.git("add", "-A", cwd=self.wt)
        self.git("commit", "-q", "-m", "rescued: preserved work", cwd=self.wt)
        (self.target / "new.txt").write_text("newer main\n")
        self.git("add", "new.txt")
        self.git("commit", "-q", "-m", "main moved on")

        ok, why = holophyte.loop.reuse_leftover(self.tgt, self.wt, self.branch)

        self.assertTrue(ok, why)
        # The ancestor invariant both review routes enforce, asked of git
        # itself: raises if main is not contained in the reused HEAD.
        self.git("merge-base", "--is-ancestor", "main", "HEAD", cwd=self.wt)
        self.assertIn("rescued: preserved work",
                      self.git("log", "--format=%s", cwd=self.wt))

    def test_conflicting_preserved_commits_are_refused_with_the_tree_intact(self):
        self.leftover_worktree()
        (self.wt / "README.md").write_text("preserved line\n")
        self.git("add", "-A", cwd=self.wt)
        self.git("commit", "-q", "-m", "rescued: conflicting work", cwd=self.wt)
        tip = self.git("rev-parse", "HEAD", cwd=self.wt).strip()
        (self.target / "README.md").write_text("main's line\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "main moved on")

        ok, why = holophyte.loop.reuse_leftover(self.tgt, self.wt, self.branch)

        self.assertFalse(ok)
        self.assertIn("conflict", why)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.wt).strip(),
                         tip)
        self.assertEqual((self.wt / "README.md").read_text(),
                         "preserved line\n")

    def test_a_detached_worktree_over_a_diverged_branch_is_refused(self):
        """`checkout -B` moves the branch to HEAD, so a worktree a human
        detached — to compare the leftover against main, say — must not
        silently orphan the branch's preserved commits."""
        self.leftover_worktree()
        (self.wt / "work.txt").write_text("preserved\n")
        self.git("add", "-A", cwd=self.wt)
        self.git("commit", "-q", "-m", "rescued: preserved work", cwd=self.wt)
        tip = self.git("rev-parse", "HEAD", cwd=self.wt).strip()
        self.git("checkout", "-q", "--detach", "main", cwd=self.wt)

        ok, why = holophyte.loop.reuse_leftover(self.tgt, self.wt, self.branch)

        self.assertFalse(ok)
        self.assertIn(self.branch, why)
        self.assertEqual(
            self.git("rev-parse", self.branch, cwd=self.wt).strip(), tip)


if __name__ == "__main__":
    unittest.main()
