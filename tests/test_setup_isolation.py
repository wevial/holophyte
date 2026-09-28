"""Worktree setup runs on the same route as the target's verify commands."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from holophyte.claim import run_worktree_setup

WRITE_HOME = 'printf %s "$HOME" > home.txt'


class SetupIsolationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.table = {"agents": {"implementer": "agent-cli"}}
        self.target = SimpleNamespace(
            config=lambda: self.table, config_path=self.root / "config.toml"
        )

    def make_worktree(self):
        def git(cwd, *args):
            subprocess.run(["git", *args], cwd=cwd, check=True)

        main = self.root / "main"
        main.mkdir()
        git(main, "init", "-q", "-b", "main")
        git(main, "config", "user.name", "Configured Author")
        git(main, "config", "user.email", "author@example.test")
        git(main, "commit", "--allow-empty", "-qm", "base")
        worktree = self.root / "task"
        git(main, "worktree", "add", "-qb", "task", str(worktree))
        self.target.path = main
        return worktree

    def configure(self, isolation, setup):
        self.table["agents"]["implementer_isolation"] = isolation
        self.table["worktree"] = {"setup": setup}

    def require_docker(self):
        if os.environ.get("HOLOPHYTE_TEST_DOCKER") != "1":
            self.skipTest("set HOLOPHYTE_TEST_DOCKER=1 for container integration")
        if not shutil.which("docker"):
            self.skipTest("Docker absent")

    def test_host_route_runs_setup_with_the_host_home(self):
        worktree = self.make_worktree()
        self.configure("none", [WRITE_HOME])

        self.assertEqual(run_worktree_setup(self.target, worktree), (True, ""))
        self.assertEqual((worktree / "home.txt").read_text(), os.environ["HOME"])

    def test_container_route_runs_setup_in_the_image(self):
        self.require_docker()
        worktree = self.make_worktree()
        self.configure("container", [WRITE_HOME])

        self.assertEqual(run_worktree_setup(self.target, worktree), (True, ""))
        self.assertEqual((worktree / "home.txt").read_text(), "/home/implementer")

    def test_container_setup_failure_names_the_command(self):
        self.require_docker()
        worktree = self.make_worktree()
        self.configure("container", ["exit 3"])

        ok, report = run_worktree_setup(self.target, worktree)

        self.assertFalse(ok)
        self.assertIn("worktree setup command 1 of 1 FAILED: exit 3", report)


if __name__ == "__main__":
    unittest.main()
