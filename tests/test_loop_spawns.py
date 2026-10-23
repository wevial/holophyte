"""The processes the full-loop fixtures and the turn-end sweep start, counted
with a `subprocess.Popen` spy that calls through to the real one."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit, Idle, Reply  # noqa: E402
from loop_fixture import LoopFixture, MergeModeFixture, template_repo  # noqa: E402

from holophyte.loop import implement  # noqa: E402

# Fixed so the two runs of one scenario make the same commits.
SAME_CLOCK = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
              "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}


@contextmanager
def spawns():
    """Every argv started under it, in order; each process still runs."""
    started = []
    real = subprocess.Popen

    class Spy(real):
        def __init__(self, args, *rest, **kwargs):
            started.append([os.path.basename(str(args[0])), *map(str, args[1:])]
                           if not isinstance(args, (str, bytes)) else [args])
            super().__init__(args, *rest, **kwargs)

    with patch.object(subprocess, "Popen", Spy):
        yield started


def heads(started, *head):
    return [argv for argv in started if argv[:len(head)] == list(head)]


class PullRequestScenario(MergeModeFixture):
    """Open, one babysit pass that fixes and resolves one thread, merge."""

    def played(self, gh_on_path):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]), self.pr_state()],
                        gh_on_path=gh_on_path)
        with patch.dict(os.environ, SAME_CLOCK), spawns() as started:
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      Reply("THREAD 1: ADDRESS -- a real crash"),
                      Commit("fix: default load()"), APPROVE, Idle(""),
                      provider=self.provider())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        return self.recorded(), self.api_calls(), heads(started, "gh", "api")


def fixture(case, kind):
    made = kind()
    made.setUp()
    case.addCleanup(made.doCleanups)
    return made


class InProcessGitHubTests(unittest.TestCase):
    def test_the_in_process_fake_answers_as_the_shell_one_without_spawning(self):
        shell_lines, shell_api, shell_spawns = fixture(
            self, PullRequestScenario).played(gh_on_path=True)
        lines, api, spawned = fixture(self, PullRequestScenario).played(
            gh_on_path=False)

        api_lines = [line for line in shell_lines if line.startswith("gh api ")]
        self.assertEqual(len(shell_spawns), len(api_lines))
        self.assertEqual([kind for kind, _ in shell_api],
                         ["state", "reply", "resolve", "state", "merge"])
        self.assertEqual(lines, shell_lines)
        self.assertEqual(api, shell_api)
        self.assertEqual(spawned, [])


class TemplateRepositoryTests(unittest.TestCase):
    def test_two_setups_start_no_git_and_share_only_the_base(self):
        template_repo()
        with spawns() as started:
            first = fixture(self, LoopFixture)
            second = fixture(self, LoopFixture)

        self.assertEqual(heads(started, "git"), [])
        (first.target / "README.md").write_text("first only\n")
        first.git("commit", "-qam", "first only")
        only = first.git("rev-parse", "HEAD").strip()
        missing = subprocess.run(["git", "cat-file", "-e", f"{only}^{{commit}}"],
                                 cwd=second.target, capture_output=True)
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(second.git("rev-parse", "main").strip(), second.base)


class SweepTreeTests(LoopFixture):
    def sweep(self, wt):
        with spawns() as started:
            swept = implement._sweep_tree(self.project, None, None, wt,
                                          "stopped", False)
        return swept, started

    def test_a_clean_tree_is_swept_with_one_rev_parse(self):
        swept, started = self.sweep(self.target)

        self.assertFalse(swept)
        self.assertEqual(len(heads(started, "git", "rev-parse")), 1)
        self.assertEqual(len(heads(started, "git", "status")), 1)

    def test_a_tree_mid_merge_is_left_alone_when_merges_is_false(self):
        self.git("checkout", "-qb", "task")
        (self.target / "README.md").write_text("task\n")
        self.git("commit", "-qam", "task side")
        self.git("checkout", "-q", "main")
        (self.target / "README.md").write_text("main\n")
        self.git("commit", "-qam", "main side")
        self.git("checkout", "-q", "task")
        merged = subprocess.run(["git", "merge", "-q", "main"], cwd=self.target,
                                capture_output=True)
        self.assertNotEqual(merged.returncode, 0)
        merge_head = self.target / ".git" / "MERGE_HEAD"
        before = (self.git("rev-parse", "HEAD"), self.git("ls-files", "-s"),
                  merge_head.read_text())

        swept, _ = self.sweep(self.target)

        self.assertFalse(swept)
        self.assertEqual((self.git("rev-parse", "HEAD"),
                          self.git("ls-files", "-s"), merge_head.read_text()),
                         before)

    def test_an_unborn_head_is_not_swept(self):
        with tempfile.TemporaryDirectory() as unborn:
            subprocess.run(["git", "init", "-q", unborn], check=True)
            (Path(unborn) / "HEAD").write_text("a file named like the ref\n")

            swept, _ = self.sweep(unborn)

            self.assertFalse(swept)


if __name__ == "__main__":
    unittest.main()
