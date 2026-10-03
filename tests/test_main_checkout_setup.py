"""The detached main a main-side verify runs on gets the project's worktree
setup whenever carrying did not cover it, and a setup that fails is not
read as a red main."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import babysit_fixture as cases  # noqa: E402
from fake_agent import Commit, Idle  # noqa: E402
from loop_fixture import MergeModeFixture, StubProvider, a_task  # noqa: E402

import holophyte.config.project  # noqa: E402
from holophyte.babysit.main_checkout import detached_main  # noqa: E402


class DetachedMainSetupTests(unittest.TestCase):
    """`detached_main` over a real repository and its task worktree."""

    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Setup", "GIT_AUTHOR_EMAIL": "setup@example.com",
            "GIT_COMMITTER_NAME": "Setup",
            "GIT_COMMITTER_EMAIL": "setup@example.com"}))
        self.repo = root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("base\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.config = root / "config.toml"
        self.project = holophyte.config.project.Project(
            path=self.repo, holo_dir=root, store_path=root / "store.db",
            config_path=self.config, worktrees=root / "wts")
        self.wt = root / "wts" / "ko-1"
        self.git("worktree", "add", "-q", "-b", "task/ko-1", str(self.wt), "main")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    def prepare(self, worktree_table):
        """What the yielded tree holds, read while it is still checked out."""
        self.config.write_text("[worktree]\n" + worktree_table)
        sha = self.git("rev-parse", "main")
        with detached_main(self.project, None, None, 1, self.wt, sha) as (
                tree, setup_failure):
            return {"marker": (tree / "MARKER").exists(),
                    "deps_link": (tree / "deps").is_symlink(),
                    "deps_target": (tree / "deps").resolve(),
                    "setup_failure": setup_failure}

    def test_setup_runs_on_main_when_the_project_carries_nothing(self):
        seen = self.prepare('setup = ["touch MARKER"]\n')
        self.assertTrue(seen["marker"])
        self.assertIsNone(seen["setup_failure"])

    def test_a_carried_directory_is_linked_and_setup_does_not_run(self):
        (self.wt / "deps").mkdir()
        seen = self.prepare('setup = ["touch MARKER"]\ncarry = ["deps"]\n')
        self.assertTrue(seen["deps_link"])
        self.assertEqual(seen["deps_target"], (self.wt / "deps").resolve())
        self.assertFalse(seen["marker"])


class MainSideSetupBabysitTests(cases.ConflictingMainHelpers, MergeModeFixture):
    """A merged tree that fails verify, then main verified with no `carry`."""

    def setup_candidate(self, command, setup):
        """A candidate adding THING.md, a target whose `[worktree] setup` is
        `setup` with no `carry`, and a main moved on by MOVED.md."""
        self.parked_on_a_nit(Commit("candidate", path="THING.md"))
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       f"[worktree]\nsetup = {json.dumps(setup)}\n")
        self.provider = lambda: StubProvider(
            dict(a_task(), body=self.BODY, verify=command))
        moved = self.remote_main("MOVED.md", "main moved\n")
        self.serve(self.pr_state(mergeable="CONFLICTING"), self.pr_state())
        return moved

    def prepared(self):
        return [event for (event,) in self.read(
            "SELECT summary FROM runEvents WHERE runId = 2"
            " AND kind = 'verification'"
            " AND summary LIKE 'main-side verify%prepared%'")]

    def test_a_failed_setup_on_main_goes_to_the_fix_round_not_red_main(self):
        command = "test -e FIXED.md"
        moved = self.setup_candidate(command, ["echo no-venv >&2; exit 3"])
        fake, _ = self.resume(Commit("fix merge", path="FIXED.md"), Idle(""))
        self.assertEqual(fake.roles[0], "implement")
        goal = fake.turns[0].goal
        self.assertIn(f"main at {moved} was not verified", goal)
        self.assertIn("worktree setup failed", goal)
        self.assertNotIn("main is red", goal)
        self.assertNotIn("main is red", self.question())
        (event,) = self.prepared()
        self.assertIn("ran setup: echo no-venv >&2; exit 3 -- FAILED", event)

    def test_main_red_after_a_successful_setup_still_parks(self):
        command = "test -d .venv && test ! -e MOVED.md"
        moved = self.setup_candidate(command, ["mkdir .venv"])
        fake, _ = self.resume()
        self.assertEqual(fake.roles, [])
        self.assertIn(f"main is red at {moved}; verify command: {command}",
                      self.question())
        (event,) = self.prepared()
        self.assertIn("ran setup: mkdir .venv", event)
        self.assertNotIn("FAILED", event)


if __name__ == "__main__":
    unittest.main()
