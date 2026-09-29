"""A babysit conflict turn under a concurrent fetch, with real `git` fetches.

GitHub's repository is a local bare clone of the fixture target; the
babysitter's `git fetch origin` and the one another worker makes during the
implementer turn both fetch from it for real, into the remote-tracking refs
the task worktree shares with the target checkout.
"""
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fake_agent import APPROVE, Commit, Idle, Reply  # noqa: E402
from loop_fixture import BRANCH, MergeModeFixture  # noqa: E402

import holophyte.cli.operator  # noqa: E402


class ConflictTurnUnderAConcurrentFetchTests(MergeModeFixture):

    def setUp(self):
        super().setUp()
        self.remote = self.worktrees.parent / "github.git"
        self.git("clone", "-q", "--bare", str(self.target), str(self.remote))

    def advance_remote_main(self, path, body):
        """Commit `body` at `path` on the bare repository's `main` with a
        temporary index and plumbing; returns the new `main` sha."""
        index = self.worktrees.parent / "remote-main-index"
        env = dict(os.environ, GIT_INDEX_FILE=str(index),
                   GIT_AUTHOR_NAME="Remote", GIT_AUTHOR_EMAIL="r@example.invalid",
                   GIT_COMMITTER_NAME="Remote",
                   GIT_COMMITTER_EMAIL="r@example.invalid")

        def plumb(*args, **kw):
            return subprocess.run(
                ["git", *args], cwd=self.remote, env=env, check=True,
                capture_output=True, text=True, **kw).stdout.strip()

        plumb("read-tree", "main")
        blob = plumb("hash-object", "-w", "--stdin", input=body)
        plumb("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
        moved = plumb("commit-tree", plumb("write-tree"), "-p", "main",
                      "-m", "main moved on")
        index.unlink(missing_ok=True)
        plumb("update-ref", "refs/heads/main", moved)
        return moved

    def park_on_a_nit(self, work):
        """Park the run on its pull request under `approve = "human"`;
        returns the approved candidate's sha."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route(states=[self.pr_state([self.NIT])],
                        fetch_from=self.remote)
        self.loop(work, APPROVE, Idle(""),
                  Reply("THREAD 1: DECLINE -- a naming preference"),
                  provider=self.provider())
        for path in self.api_dir.iterdir():
            path.unlink()
        return self.git("rev-parse", BRANCH).strip()

    def test_a_resolved_merge_is_pushed_when_main_is_fetched_during_the_turn(self):
        approved = self.park_on_a_nit(
            Commit("the scripted work", path="README.md",
                   body="the branch's line\n"))
        fetched = self.advance_remote_main("README.md", "the remote's line\n")
        self.serve(self.pr_state(mergeable="CONFLICTING"), self.pr_state())
        fixture = self
        later = []

        class ResolveThenFetch(Commit):
            def play(self, cwd, turn):
                said = super().play(cwd, turn)
                later.append(fixture.advance_remote_main(
                    "LATER.md", "main moved again\n"))
                fixture.git("fetch", "origin")
                return said

        holophyte.cli.operator.babysit_ticket(
            self.project, "KO-131", "sent back to the babysitter",
            out=io.StringIO())
        fake, _ = self.loop(
            ResolveThenFetch("resolve the merge", path="README.md",
                             body="both lines\n"),
            Idle(""), provider=self.provider())

        self.assertNotIn("left it unresolved", self.question())
        wt = self.worktrees / "ko-131-add-a-thing"
        head = self.git("rev-parse", "HEAD", cwd=wt).strip()
        self.assertEqual(self.pushed(), [(BRANCH, approved), (BRANCH, head)])
        self.assertEqual(self.git("rev-parse", "HEAD^1", cwd=wt).strip(),
                         approved)
        self.assertEqual(self.git("rev-parse", "HEAD^2", cwd=wt).strip(),
                         fetched)
        self.assertEqual(fake.roles[0], "implement")
        self.assertEqual(
            self.git("rev-parse", "refs/remotes/origin/main").strip(),
            later[0])
        self.assertEqual(
            self.read("SELECT text FROM ledger WHERE kind = 'note' AND"
                      " text LIKE 'Merged main into%'"),
            [(f"Merged main into {BRANCH} at {head} (GitHub reported a"
              " conflict)",)])


if __name__ == "__main__":
    unittest.main()
