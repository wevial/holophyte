import tempfile
import unittest
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

from holophyte.pr.pullrequest import _written_pr_text
from tests.host_fixture import git


class WrittenPullRequestDiffTests(unittest.TestCase):
    def test_writer_fetches_main_before_building_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, wt, other = (root / name for name in ("origin.git", "wt", "other"))

            def commit(cwd, name):
                (cwd / name).write_text(f"{name}\n")
                git(cwd, "add", name)
                git(cwd, "commit", "-qm", name)

            git(root, "init", "--bare", "-b", "main", str(origin))
            git(root, "clone", str(origin), str(wt))
            commit(wt, "base.txt")
            git(wt, "push", "origin", "main")
            git(wt, "checkout", "-b", "task/branch")
            commit(wt, "branch-only.txt")

            git(root, "clone", str(origin), str(other))
            commit(other, "main-b.txt")
            git(other, "push", "origin", "main")
            git(wt, "fetch", str(other), "main")
            git(wt, "merge", "--no-edit", "FETCH_HEAD")
            self.assertEqual(git(wt, "rev-parse", "origin/main"),
                             git(wt, "rev-parse", "main"))

            project = SimpleNamespace(config=lambda: {})
            with patch(
                "holophyte.loop.implement._timed",
                return_value=("TITLE: Branch change\nBranch description.", False),
            ) as turn:
                _written_pr_text(
                    project, None, None, "TEST-1", "Branch change", "task/branch",
                    "Branch description.", 60, wt, monotonic(), 5, None,
                )

            prompt = turn.call_args.args[-1]
            diff = prompt.split("```diff\n", 1)[1].split("\n```", 1)[0]
            paths = [line for line in diff.splitlines()
                     if line.startswith("diff --git ")]
            self.assertEqual(paths, ["diff --git a/branch-only.txt b/branch-only.txt"])
