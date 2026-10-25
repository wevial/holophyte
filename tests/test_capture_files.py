"""Under `[merge] ui_capture_local = true`, a candidate's capture specs and
capture output stay in its worktree and never reach a pushed or merged head."""
import io
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.config.project
from holophyte.loop.gates import MergeParked
from holophyte.pr import github, pr_media, pr_status, pullrequest

DEFAULT = "console/e2e/CAPTURE-0.capture.ts"
CAPTURED = ("console/e2e/KO-7.capture.ts", "e2e/capture/KO-7.capture.ts",
            ".holophyte-capture/KO-7/01-state.png")
KEPT = ("ui/page.html", DEFAULT)
CAPTURE = """\
import pathlib, sys
if pathlib.Path("e2e/capture/KO-7.capture.ts").is_file():
    pathlib.Path(sys.argv[-1], "01-state.png").write_bytes(b"png")
"""
URL = "https://github.com/example/repo/pull/7"
MERGE_SHA = "9f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"


def publish(project, wt, output, files, task_id, note, media_repo):
    return "Media lives here.", {
        file: f"https://example.test/{file.name}" for file in files}


class Repository(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.enterContext(patch.dict(
            os.environ, {"HOLOPHYTE_HOME": str(self.tmp / "home")}))
        self.origin = self.tmp / "origin.git"
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin))
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.name", "Test implementer"),
                     ("config", "user.email", "implementer@example.test"),
                     ("commit", "--allow-empty", "-qm", "base"),
                     ("remote", "add", "origin", str(self.origin)),
                     ("push", "-q", "origin", "main")):
            self.git(*args)
        self.script = self.tmp / "capture.py"
        self.script.write_text(CAPTURE)
        self.project = holophyte.config.project.Project.locate(self.repo)

    def configure(self, local, default=DEFAULT):
        command = shlex.join([sys.executable, str(self.script),
                              "--default", str(default)])
        self.project.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.project.config_path.write_text(
            '[merge]\nmode = "pr"\nui_paths = ["ui/*"]\n'
            f'ui_capture = {command!r}\nui_capture_dir = "e2e/capture"\n'
            f"ui_capture_local = {local}\n")
        self.project._config = None

    def git(self, *args, cwd=None):
        return subprocess.check_output(
            ["git", *args], cwd=cwd or self.repo, text=True,
            stderr=subprocess.PIPE).strip()

    def add(self, cwd, path, force=False):
        (cwd / path).parent.mkdir(parents=True, exist_ok=True)
        (cwd / path).write_text(f"{path}\n")
        self.git("add", *(["-f"] if force else []), path, cwd=cwd)


class PushTests(Repository):
    def setUp(self):
        super().setUp()
        self.wt = self.tmp / "wt"
        self.git("worktree", "add", "-q", "-b", "task", str(self.wt), "main")
        (self.wt / "e2e/capture").mkdir(parents=True)
        (self.wt / "e2e/capture/.gitignore").write_text("*\n")
        for path in (*KEPT, *CAPTURED):
            self.add(self.wt, path, force=path.startswith("e2e/capture/"))
        self.git("commit", "-qm", "candidate", cwd=self.wt)

    def pushed_tree(self):
        return set(self.git("ls-tree", "-r", "--name-only", "task",
                            cwd=self.origin).splitlines())

    def test_local_capture_push_leaves_capture_files_in_the_worktree_only(self):
        self.configure("true")
        github.push_branch(self.project, "task")

        self.git("fetch", "-q", "origin")
        diff = set(self.git("diff", "--name-only", "main...origin/task").splitlines())
        for listed in (self.pushed_tree(), diff):
            self.assertLessEqual(set(KEPT), listed)
            self.assertFalse(set(CAPTURED) & listed)
        for path in CAPTURED:
            self.assertTrue((self.wt / path).is_file(), path)
        self.git("add", "-A", cwd=self.wt)
        self.assertEqual(self.git("status", "--porcelain", cwd=self.wt), "")

    def test_pushed_head_still_captures_from_the_worktree_spec(self):
        self.configure("true")
        github.push_branch(self.project, "task")

        with patch.object(pr_media, "_publish_git", publish), \
                patch("sys.stdout", io.StringIO()):
            section = pr_media.prepare(self.project, self.wt, "KO-7",
                                       evidence_states=["State shown"])

        self.assertIn("(https://example.test/01-state.png)", section)

    def test_the_default_spec_is_pushed_under_any_spelling_of_its_path(self):
        for default in (self.wt / DEFAULT, f"console/../{DEFAULT}"):
            with self.subTest(default=default):
                self.configure("true", default)
                github.push_branch(self.project, "task")

                self.assertIn(DEFAULT, self.pushed_tree())
                self.assertFalse(set(CAPTURED) & self.pushed_tree())

    def test_a_later_push_without_capture_files_adds_no_commit(self):
        self.configure("true")
        github.push_branch(self.project, "task")
        first = self.git("rev-parse", "task", cwd=self.origin)
        self.add(self.wt, "ui/other.html")
        self.git("commit", "-qm", "more", cwd=self.wt)

        github.push_branch(self.project, "task")

        head = self.git("rev-parse", "HEAD", cwd=self.wt)
        self.assertEqual(self.git("rev-parse", "task", cwd=self.origin), head)
        self.assertEqual(self.git("rev-parse", "HEAD^", cwd=self.wt), first)

    def test_committed_capture_mode_pushes_every_added_file(self):
        self.configure("false")
        github.push_branch(self.project, "task")

        self.assertLessEqual({*KEPT, *CAPTURED}, self.pushed_tree())


class MergeTests(Repository):
    def candidate(self, *paths):
        self.git("checkout", "-qb", "task/ko-7")
        for path in paths:
            self.add(self.repo, path)
        self.git("commit", "-qm", "candidate")
        self.git("push", "-q", "origin", "task/ko-7")
        return self.git("rev-parse", "HEAD")

    def merge(self, sha):
        self.configure("true")
        self.merged = []

        def merge_pull_request(project, pull, pinned):
            self.merged.append(pinned)
            return MERGE_SHA

        with patch.object(pullrequest.merge_queue, "merge_queue_required",
                          return_value=False), \
                patch.object(github, "merge_pull_request", merge_pull_request), \
                patch.object(pullrequest, "retire_merged_worktree"), \
                patch("sys.stdout", io.StringIO()):
            return pullrequest._merge_pr(
                self.project, None, None, None, "KO-7", "task/ko-7", self.repo,
                sha, 1, pr_status.parse_pr_url(URL))

    def test_a_head_adding_a_capture_spec_parks_unmerged(self):
        sha = self.candidate("ui/page.html", "console/e2e/KO-7.capture.ts")

        with self.assertRaises(MergeParked) as parked:
            self.merge(sha)

        self.assertIn("console/e2e/KO-7.capture.ts", str(parked.exception))
        self.assertEqual(self.merged, [])

    def test_a_head_without_capture_files_merges(self):
        sha = self.candidate("ui/page.html")

        self.assertEqual(self.merge(sha), MERGE_SHA)
        self.assertEqual(self.merged, [sha])


if __name__ == "__main__":
    unittest.main()
