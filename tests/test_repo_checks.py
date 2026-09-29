"""Repo-wide suite modules as a `[verify] always` baseline, and the brief."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import holophyte.gates  # noqa: E402 - after the sys.path insert above
import holophyte.project  # noqa: E402 - after the sys.path insert above
from tests.fake_agent import APPROVE, Commit  # noqa: E402
from tests.loop_fixture import LoopFixture  # noqa: E402

SIZES = "python3 tests/run_modules.py tests/test_file_sizes.py"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


class FileSizesBaselineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "repo"
        self.repo.mkdir()
        names = git(ROOT, "ls-files", "--cached", "--others",
                    "--exclude-standard").splitlines()
        for name in names:
            source = ROOT / name
            if source.is_file() and not source.is_symlink():
                (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, self.repo / name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "factory@example.invalid")
        git(self.repo, "config", "user.name", "Factory Test")
        git(self.repo, "config", "gc.auto", "0")
        git(self.repo, "config", "maintenance.auto", "false")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "checkout")
        self.enterContext(patch.dict(
            os.environ, HOLOPHYTE_HOME=str(Path(tmp.name) / "home")))
        state = holophyte.project.state_dir(self.repo)
        state.mkdir(parents=True)
        (state / "config.toml").write_text(f"[verify]\nalways = [{SIZES!r}]\n")
        self.project = holophyte.project.Project.locate(self.repo)

    def baseline(self):
        return holophyte.gates.with_baseline(self.project, self.repo, "",
                                             True, "")

    def test_the_temporary_repository_turns_off_automatic_maintenance(self):
        self.assertEqual(git(self.repo, "config", "--get", "gc.auto"), "0\n")
        self.assertEqual(
            git(self.repo, "config", "--get", "maintenance.auto"), "false\n")

    def test_a_file_grown_past_its_ceiling_fails_the_baseline_naming_the_module(self):
        ok, out = self.baseline()
        self.assertTrue(ok, str(out)[-2000:])
        self.assertNotIn("failed: test_file_sizes.py", str(out))

        grown = self.repo / "holophyte" / "reexec.py"
        grown.write_text(grown.read_text() + "\n" * 1000)
        git(self.repo, "commit", "-q", "-am", "grow a file past its ceiling")
        ok, out = self.baseline()
        self.assertFalse(ok)
        self.assertIn("failed: test_file_sizes.py", str(out)[-2000:])


class BaselineBriefTests(LoopFixture):
    def test_implementer_brief_lists_always_commands_after_the_ticket_verify(self):
        self.configure('[verify]\nalways = ["echo baseline-sentinel"]\n')
        fake, _ = self.loop(Commit("candidate"), APPROVE)
        goal = fake.turns[0].goal
        self.assertIn("echo ok", goal)
        self.assertIn("echo baseline-sentinel", goal)
        self.assertLess(goal.index("echo ok"),
                        goal.index("echo baseline-sentinel"))
        self.assertLess(goal.index("echo baseline-sentinel"),
                        goal.index("Run only the commands listed above"))

    def test_brief_without_always_commands_mentions_no_baseline(self):
        fake, _ = self.loop(Commit("candidate"), APPROVE)
        goal = fake.turns[0].goal
        self.assertIn("echo ok", goal)
        self.assertNotIn("baseline", goal.lower())


if __name__ == "__main__":
    unittest.main()
