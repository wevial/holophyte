"""`/shipped` rows carry `commit_url` when the merge sha is on `origin`.

The target is a real repository with a bare second repository as its
`origin`: one merge commit pushed to `origin/main`, one made locally after
and never pushed. What is asserted is what the console would see over the
socket, with git as the oracle for which sha reached the remote.

Run: python3 -m unittest discover -s tests -p 'test_serve_shipped*' -v
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from time import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_serve import MIN, ServeTestCase  # noqa: E402 - after the insert

import store  # noqa: E402 - after the sys.path insert above

GIT_IDENTITY = ("-c", "user.name=test", "-c", "user.email=test@example.com",
                "-c", "commit.gpgsign=false")


class CommitUrlTests(ServeTestCase):
    """`commit_url` on `/shipped` rows and `/runs/N`."""

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *GIT_IDENTITY, *args],
                              cwd=cwd or self.target, check=True,
                              capture_output=True, text=True).stdout.strip()

    def build_repository(self, origin_url=None):
        """A checkout on `main` with two `--no-ff` merges: `self.pushed`,
        on `origin/main` when `origin_url` names the bare repository the
        test made, and `self.local`, committed afterwards and never
        pushed. `origin_url` other than the bare path is set after the
        push, so the rows are judged by the URL shape, not by a fetch."""
        self.git("init", "-q", "-b", "main")
        (self.target / "README").write_text("one\n")
        self.git("add", "README")
        self.git("commit", "-q", "-m", "one")
        for name in ("pushed", "local"):
            self.git("checkout", "-q", "-b", name)
            (self.target / name).write_text(f"{name}\n")
            self.git("add", name)
            self.git("commit", "-q", "-m", name)
            self.git("checkout", "-q", "main")
            self.git("merge", "-q", "--no-ff", "-m", f"merge {name}", name)
            setattr(self, name, self.git("rev-parse", "HEAD"))
            if name == "pushed" and origin_url is not None:
                bare = self.root / "origin.git"
                self.git("init", "-q", "--bare", str(bare))
                self.git("remote", "add", "origin", str(bare))
                self.git("push", "-q", "origin", "main")
                self.git("remote", "set-url", "origin", origin_url)

    def seed_merged(self, shas):
        """One merged run per sha, ended a minute apart, oldest first."""
        self.now = int(time() * 1000)
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.ensure_project(conn, "team-1", self.target)
            self.runs = {}
            for n, sha in enumerate(shas, start=1):
                ticket = store.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{n}",
                    linear_identifier=f"KO-{n}", title=f"ticket {n}",
                    acceptance_criteria=[f"Given {n}, then it is worked"],
                    verification_commands=["echo ok"], time_box_ms=30 * MIN)
                store.transition(conn, ticket, "in_flight")
                run = store.claim(conn, project, ticket,
                                  now=self.now - (10 - n) * MIN)
                store.release(conn, run, "merged", now=self.now - (5 - n) * MIN,
                              merge_sha=sha)
                self.runs[sha] = run
        finally:
            conn.close()

    def test_links_a_pushed_merge(self):
        self.build_repository("https://github.com/example/repo.git")
        self.seed_merged([self.pushed])
        self.start()

        code, _, body = self.request("GET", "/shipped")

        self.assertEqual(code, 200)
        [row] = body["rows"]
        self.assertEqual(row["merge_sha"], self.pushed)
        self.assertEqual(
            row["commit_url"],
            f"https://github.com/example/repo/commit/{self.pushed}")

        code, _, detail = self.request("GET", f"/runs/{row['id']}")
        self.assertEqual(code, 200)
        self.assertEqual(detail["run"]["commit_url"], row["commit_url"])

    def test_unpushed_and_originless_rows_do_not_link(self):
        self.build_repository("https://github.com/example/repo.git")
        self.seed_merged([self.pushed, self.local, None])
        self.start()

        code, _, body = self.request("GET", "/shipped")

        self.assertEqual(code, 200)
        by_sha = {row["merge_sha"]: row for row in body["rows"]}
        self.assertEqual(set(by_sha), {self.pushed, self.local, None})
        self.assertIsNotNone(by_sha[self.pushed]["commit_url"])
        self.assertIsNone(by_sha[self.local]["commit_url"])
        self.assertIsNone(by_sha[None]["commit_url"])
        # The row is otherwise the row: the same keys, the same values.
        linked = dict(by_sha[self.pushed], commit_url=None, merge_sha=None,
                      id=None, ticket=None, title=None, started_ms=None,
                      ended_ms=None)
        unlinked = dict(by_sha[self.local], commit_url=None, merge_sha=None,
                        id=None, ticket=None, title=None, started_ms=None,
                        ended_ms=None)
        self.assertEqual(linked, unlinked)
        self.assertEqual(
            set(by_sha[self.local]),
            {"id", "ticket", "title", "rounds", "findings", "started_ms",
             "ended_ms", "actual_min", "estimate_min", "merge_sha",
             "commit_url", "host"})

        # Without an `origin` at all, the pushed sha links nowhere either.
        self.git("remote", "remove", "origin")
        code, _, body = self.request("GET", "/shipped")
        self.assertEqual(code, 200)
        self.assertEqual([row["commit_url"] for row in body["rows"]],
                         [None, None, None])
        self.assertEqual({row["merge_sha"] for row in body["rows"]},
                         {self.pushed, self.local, None})

    def test_ssh_remote_normalizes(self):
        self.build_repository("git@github.com:example/repo.git")
        self.seed_merged([self.pushed])
        self.start()

        code, _, body = self.request("GET", "/shipped")

        self.assertEqual(code, 200)
        self.assertEqual(
            body["rows"][0]["commit_url"],
            f"https://github.com/example/repo/commit/{self.pushed}")

    def test_a_remote_of_another_shape_does_not_link(self):
        self.build_repository("ssh://git.example.org/~user/repo.git")
        self.seed_merged([self.pushed])
        self.start()

        code, _, body = self.request("GET", "/shipped")

        self.assertEqual(code, 200)
        self.assertIsNone(body["rows"][0]["commit_url"])


if __name__ == "__main__":
    import unittest
    unittest.main()
