"""The launch-seam fixture the isolation test modules share."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class IsolationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.table = {"agents": {"implementer": "agent-cli"}}
        self.target = SimpleNamespace(
            config=lambda: self.table, config_path=self.root / "config.toml"
        )

    def make_worktree(self):
        from holophyte.isolation.isolation_git import git

        main = self.root / "main"
        main.mkdir()
        git(main, "init", "-q", "-b", "main")
        git(main, "config", "user.name", "Configured Author")
        git(main, "config", "user.email", "author@example.test")
        git(main, "commit", "--allow-empty", "-qm", "base")
        worktree = self.root / "task"
        git(main, "worktree", "add", "-qb", "task", str(worktree))
        return main, worktree

    def carry_worktree(self, carry):
        from holophyte.isolation.isolation_git import git

        main, worktree = self.make_worktree()
        (worktree / ".gitignore").write_text("deps/\nnode_modules/\nmissing/\n")
        (worktree / "tracked").mkdir()
        (worktree / "tracked" / "file").write_text("tracked\n")
        git(worktree, "add", ".")
        git(worktree, "commit", "-qm", "ignore installs")
        self.target.path = main
        self.table["worktree"] = {"carry": carry}
        return worktree

    def recorded_volumes(self, launch, turn=lambda clone: None):
        from holophyte.isolation import launcher

        volumes = []

        def run(argv, cwd, timeout, *, env):
            volumes.extend(argv[i + 1] for i, flag in enumerate(argv)
                           if flag == "--volume")
            turn(Path(cwd))
            return 0, "done"

        with patch.object(launcher, "image_ready"), \
             patch.object(launcher.review_runner, "_remove_container"), \
             patch.object(launcher, "run_capped", side_effect=run):
            return launch(), volumes

    def fake_codex_release(self, names=("codex", "codex-code-mode-host")):
        release = self.root / "release"
        release.mkdir()
        for name in names:
            (release / name).write_text("#!/bin/sh\n")
            (release / name).chmod(0o755)
        return release
