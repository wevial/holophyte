import tempfile
import unittest
from pathlib import Path

from holophyte.isolation import isolation_clone
from holophyte.isolation.isolation_git import git


class TurnCloneMainRefsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        self.primary = self.root / "primary"
        git(self.root, "clone", "-q", str(origin), str(self.primary))
        self.identify(self.primary)
        git(self.primary, "commit", "--allow-empty", "-qm", "base")
        git(self.primary, "push", "-q", "origin", "main")
        self.base = git(self.primary, "rev-parse", "HEAD")
        upstream = self.root / "upstream"
        git(self.root, "clone", "-q", str(origin), str(upstream))
        self.identify(upstream)
        git(upstream, "commit", "--allow-empty", "-qm", "upstream")
        git(upstream, "push", "-q", "origin", "main")
        git(self.primary, "fetch", "-q", "origin")
        self.worktree = self.root / "task"
        git(self.primary, "worktree", "add", "-qb", "task", str(self.worktree),
            self.base)
        for subject in ("task one", "task two"):
            git(self.worktree, "commit", "--allow-empty", "-qm", subject)

    def identify(self, repository):
        git(repository, "config", "user.name", "Configured Author")
        git(repository, "config", "user.email", "author@example.test")

    def main_refs(self, repository):
        return git(repository, "for-each-ref", "--format=%(refname) %(objectname)",
                   "refs/remotes/origin/main", "refs/heads/main").splitlines()

    def test_clone_finds_the_task_base_against_the_worktrees_main(self):
        expected = self.main_refs(self.worktree)
        self.assertEqual(len(expected), 2)
        with isolation_clone.turn_clone(self.worktree) as (clone, _):
            self.assertEqual(self.main_refs(clone), expected)
            self.assertEqual(git(clone, "merge-base", "HEAD", "origin/main"),
                             self.base)
            self.assertEqual(git(clone, "symbolic-ref", "HEAD"), "refs/heads/task")

    def test_return_brings_back_the_task_commit_and_leaves_main_refs(self):
        before = self.main_refs(self.worktree)
        with isolation_clone.turn_clone(self.worktree) as (clone, _):
            git(clone, "commit", "--allow-empty", "-qm", "container work")
            made = git(clone, "rev-parse", "HEAD")
        self.assertEqual(self.main_refs(self.worktree), before)
        self.assertEqual(git(self.worktree, "rev-parse", "task"), made)

    def test_repository_without_a_local_main_still_carries_origin_main(self):
        git(self.primary, "checkout", "-q", "--detach")
        git(self.primary, "branch", "-qD", "main")
        tracking = git(self.worktree, "rev-parse", "refs/remotes/origin/main")
        with isolation_clone.turn_clone(self.worktree) as (clone, _):
            self.assertEqual(self.main_refs(clone),
                             [f"refs/remotes/origin/main {tracking}"])


if __name__ == "__main__":
    unittest.main()
