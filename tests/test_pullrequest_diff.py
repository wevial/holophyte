import subprocess
import tempfile
import unittest
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

from holophyte.pr.pullrequest import _written_pr_text


class WrittenPullRequestDiffTests(unittest.TestCase):
    def test_writer_diff_excludes_main_changes_merged_into_branch(self):
        self.check_writer_diff(stale=False)

    def test_writer_fetches_main_before_building_diff(self):
        self.check_writer_diff(stale=True)

    def check_writer_diff(self, *, stale):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, wt, other = (root / name for name in ("origin.git", "wt", "other"))

            def git(cwd, *args):
                return subprocess.run(
                    ["git", "-c", "user.name=Test", "-c",
                     "user.email=test@example.invalid", "-c",
                     "commit.gpgsign=false", *args],
                    cwd=cwd, check=True, capture_output=True, text=True,
                ).stdout.strip()

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
            commit(other, "main-c.txt")
            git(other, "push", "origin", "main")
            if stale:
                git(wt, "fetch", str(other), "main")
                git(wt, "merge", "--no-edit", "FETCH_HEAD")
                self.assertEqual(git(wt, "rev-parse", "origin/main"),
                                 git(wt, "rev-parse", "main"))
            else:
                git(wt, "fetch", "origin")
                git(wt, "merge", "--no-edit", "origin/main")
                self.assertEqual(
                    git(wt, "rev-list", "--count", "main..origin/main"), "2")
                self.assertEqual(git(wt, "diff", "--name-only", "origin/main...HEAD"),
                                 "branch-only.txt")

            project = SimpleNamespace(path=wt, config_path=root / "config.toml",
                                      config=lambda: {})
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
