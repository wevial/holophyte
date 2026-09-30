"""`_merge_pr()` on a candidate behind a real `origin`'s `main`, with and
without `[merge] require_up_to_date`."""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import holophyte.loop.gates  # noqa: E402 - after the sys.path insert above
import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import holophyte.pr.merge_queue  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pr_status  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pullrequest  # noqa: E402 - after the sys.path insert above

BRANCH = "task/ko-1-behind-main"
URL = "https://github.com/example/repo/pull/7"
MERGE_SHA = "9f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"


class BehindMainMergeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.origin = self.root / "origin.git"
        self.wt = self.root / "wt"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin),
                 cwd=tmp.name)
        self.git("clone", "-q", str(self.origin), str(self.wt), cwd=tmp.name)
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "T")
        self.git("checkout", "-qb", "main")
        self.commit("BASE.md")
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-qb", BRANCH)
        self.sha = self.commit("CANDIDATE.md")
        self.git("checkout", "-q", "main")
        self.commit("MOVED.md")
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-q", BRANCH)
        self.merged = []

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.wt, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, path):
        (self.wt / path).write_text(f"{path}\n")
        self.git("add", path)
        self.git("commit", "-qm", path)
        return self.git("rev-parse", "HEAD")

    def merge(self, merge_table):
        project = SimpleNamespace(
            path=self.wt, config_path=self.root / "config.toml",
            config=lambda: {"merge": merge_table})
        pull = holophyte.pr.pr_status.parse_pr_url(URL)

        def merge_pull_request(project, pull, pinned):
            self.merged.append(pinned)
            return MERGE_SHA

        with patch.object(holophyte.pr.merge_queue, "merge_queue_required",
                          return_value=False), \
                patch.object(holophyte.pr.github, "merge_pull_request",
                             merge_pull_request), \
                patch("sys.stdout", io.StringIO()):
            return holophyte.pr.pullrequest._merge_pr(
                project, None, None, None, "KO-1", BRANCH, self.wt, self.sha,
                1, pull)

    def test_require_up_to_date_false_merges_a_candidate_behind_main(self):
        merge_sha = self.merge({"require_up_to_date": False})
        self.assertEqual(self.merged, [self.sha])
        self.assertEqual(merge_sha, MERGE_SHA)

    def test_absent_key_parks_a_candidate_behind_main_unmerged(self):
        with self.assertRaises(holophyte.loop.gates.MergeParked) as parked:
            self.merge({})
        self.assertIn("is behind main", str(parked.exception))
        self.assertEqual(self.merged, [])


if __name__ == "__main__":
    unittest.main()
