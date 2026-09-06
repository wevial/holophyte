"""`/runs/N/files` for a live run reads the run's worktree.

The target is a real repository; the run's worktree is cut beside it where
the loop would put it (`holophyte.target.worktree_path()`), and the store
carries the branch as the loop records it at the cut. What is asserted is
what the console would see over the socket, with git as the oracle.

Run: python3 -m unittest discover -s tests -p 'test_serve_files*' -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_serve import ServeTestCase  # noqa: E402 - after the insert

import holophyte.target  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above

GIT_IDENTITY = ("-c", "user.name=test", "-c", "user.email=test@example.com",
                "-c", "commit.gpgsign=false")


class LiveRunFilesTests(ServeTestCase):
    """A live run's files come from its worktree: committed and uncommitted
    changes together, untracked files as additions, an empty list when
    nothing changed yet."""

    BRANCH = "task/ko-7-ticket-7"

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *GIT_IDENTITY, *args],
                              cwd=cwd or self.target, check=True,
                              capture_output=True, text=True).stdout.strip()

    def setUp(self):
        super().setUp()
        self.git("init", "-q", "-b", "main")
        (self.target / "kept.txt").write_text("one\ntwo\nthree\n")
        (self.target / "edited.txt").write_text("a\nb\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")
        self.seed()
        self.tgt = holophyte.target.Target.locate(self.target)
        self.wt = holophyte.target.worktree_path(self.tgt, self.BRANCH)
        # As the loop cuts it: detached at main, then the task branch.
        self.git("worktree", "add", "-q", "--detach", str(self.wt), "main")
        self.git("checkout", "-q", "-b", self.BRANCH, cwd=self.wt)
        conn = store.open(str(self.db))
        try:
            store.set_branch(conn, self.run, self.BRANCH)
        finally:
            conn.close()
        self.start()

    def test_worktree_changes_are_listed(self):
        (self.wt / "kept.txt").write_text("one\nTWO\nthree\n")
        self.git("commit", "-q", "-am", "committed", cwd=self.wt)
        (self.wt / "edited.txt").write_text("a\nb\nc\nd\n")
        (self.wt / "fresh.txt").write_text("x\ny\nz")

        code, headers, body = self.request("GET", f"/runs/{self.run}/files")

        self.assertEqual(code, 200, body)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body["files"], [
            {"path": "edited.txt", "status": "M", "added": 2, "deleted": 0},
            {"path": "fresh.txt", "status": "A", "added": 3, "deleted": 0},
            {"path": "kept.txt", "status": "M", "added": 1, "deleted": 1},
        ])
        self.assertEqual(body["base"], self.git("rev-parse", "main"))
        self.assertEqual(body["head"], self.git("rev-parse", "HEAD", cwd=self.wt))
        self.assertEqual((body["total_added"], body["total_deleted"]), (6, 1))
        self.assertFalse(body["truncated"])

    def test_untracked_symlink_counts_its_link_text(self):
        # A link is one line of link text to git, however long its target
        # is; a link to a FIFO must not be followed or the request blocks.
        (self.wt / "target.txt").write_text("one\ntwo\nthree\n")
        (self.wt / "link.txt").symlink_to("target.txt")
        os.mkfifo(self.wt / "pipe")
        (self.wt / "pipe-link").symlink_to("pipe")

        code, _headers, body = self.request("GET", f"/runs/{self.run}/files")

        self.assertEqual(code, 200, body)
        before = {f["path"]: (f["status"], f["added"]) for f in body["files"]}
        self.git("add", "link.txt", "pipe-link", "target.txt", cwd=self.wt)
        staged = {}
        for line in self.git("diff", "--cached", "--numstat",
                             cwd=self.wt).splitlines():
            added, _deleted, path = line.split("\t")
            staged[path] = ("A", int(added))
        self.assertEqual(staged["link.txt"], ("A", 1))
        self.assertEqual(before, staged)

    def test_empty_and_gone(self):
        code, _headers, body = self.request("GET", f"/runs/{self.run}/files")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["files"], [])
        self.assertEqual((body["total_added"], body["total_deleted"]), (0, 0))
        self.assertEqual(body["base"], body["head"])

        # The worktree and the branch both gone: nothing left to diff.
        shutil.rmtree(self.wt)
        self.git("worktree", "prune")
        self.git("branch", "-D", self.BRANCH)
        code, _headers, body = self.request("GET", f"/runs/{self.run}/files")
        self.assertEqual(code, 409, body)
        self.assertIn(self.BRANCH, body["error"])


if __name__ == "__main__":
    import unittest
    unittest.main()
