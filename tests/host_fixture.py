"""The host tests' shared fixture: a temporary home, real git repositories
with `[board]` configs under it, the CLI run in-process; and a copy of this
factory committed in its own git checkout, for the tests that move a
daemon's `HEAD` under it.

`HostFixture` lives here so `test_host*` and `test_serve_host*` share one
base class, and `factory_checkout()` so the serve tests share one copy.
"""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli
import store
from holophyte.host import Host
from holophyte.project import Project

REPO = Path(__file__).resolve().parent.parent


def git(checkout, *args):
    """Run git in `checkout` as a throwaway identity; its stdout, stripped."""
    return subprocess.run(
        ["git", "-c", "user.name=serve-test", "-c", "user.email=serve@test",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
         *args], cwd=checkout, capture_output=True, text=True,
        check=True).stdout.strip()


def factory_checkout(case, directory, check):
    """A copy of this factory committed as commit A in its own git
    repository at `directory`, its code-check interval cut to `check`
    seconds so a daemon run from it notices a new commit quickly."""
    skip = shutil.ignore_patterns("__pycache__")
    for package in ("holophyte", "store"):
        shutil.copytree(REPO / package, directory / package, ignore=skip)
    for module in REPO.glob("*.py"):
        shutil.copy(module, directory)
    watch = directory / "holophyte" / "serve_watch.py"
    text = watch.read_text()
    case.assertIn("\nCODE_CHECK_SEC = 15\n", text)
    watch.write_text(text.replace("\nCODE_CHECK_SEC = 15\n",
                                  f"\nCODE_CHECK_SEC = {check}\n"))
    git(directory, "init", "-q")
    git(directory, "add", "-A")
    git(directory, "commit", "-q", "-m", "A")
    return directory


class HostFixture(unittest.TestCase):
    """A temporary home, repositories under it, the CLI in-process."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        patcher = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def repo(self, directory, team=None, name=None):
        """A git repository at `root/directory` with a `[board]` config,
        and `[serve] name` when `name` is given."""
        path = self.root / directory
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        target = Project.locate(path, adopt=False)
        target.holo_dir.mkdir(parents=True, exist_ok=True)
        text = (f'[board]\nteam = "{team or "team-" + directory}"\n'
                f'project_id = "p-{directory}"\n')
        if name is not None:
            text += f'[serve]\nname = "{name}"\n'
        target.config_path.write_text(text)
        return path

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = holophyte.cli.cli(list(args))
        return code, out.getvalue()

    def registered(self):
        return [(entry.name, entry.path) for entry in Host.locate().projects()]

    def interventions(self, path, action):
        conn = store.open(str(path))
        try:
            return conn.execute("SELECT count(*) FROM interventions"
                                " WHERE action = ?", (action,)).fetchone()[0]
        finally:
            conn.close()
