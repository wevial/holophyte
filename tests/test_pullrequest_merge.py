"""The merge through the pull request's API."""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    Commit,
    Idle,
)
from heartbeat_fixture import (  # noqa: E402 - after the sys.path insert above
    LOADED_MS,
    SAMPLE_MS,
    heartbeat_sampler,
    patch_beats,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    MergeModeFixture,
)

import holophyte.config.config_tables  # noqa: E402 - after the sys.path insert above
import holophyte.loop.gates  # noqa: E402 - after the sys.path insert above
import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import holophyte.pr.merge_queue  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pr_status  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pullrequest  # noqa: E402 - after the sys.path insert above


class PullRequestMergeTests(MergeModeFixture):
    def test_a_squash_only_repository_merges_with_its_configured_method(self):
        """Squash uses PR metadata, pins the candidate and records the landed sha."""
        self.configure('[merge]\nmode = "pr"\npr_merge_method = "squash"\n')
        self.fake_route()

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            provider=self.provider())

        self.assertEqual(self.api_calls()[-1],
                         ("merge", {"merge_method": "squash",
                                    "commit_title": "feat(x): do y (KO-1) (#7)",
                                    "commit_message": "",
                                    "sha": fake.turns[1].candidate_sha}))
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("done", "merged", self.MERGE_SHA)])

    def slow_push(self, delay_ms=0, silent=False):
        """Park a run whose push samples its heartbeat for longer than the
        stale budget, each beat `delay_ms` late or `silent`."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       "[supervisor]\nheartbeat_stale_min = 0.01\n")
        patch_beats(self, delay_ms, silent)
        knobs = holophyte.config.config_tables.sweep_config(self.project)
        budget_s = knobs.heartbeat_stale_ms * knobs.stale_strikes / 1000
        samples = self.db.parent / "heartbeats.log"
        sampler = heartbeat_sampler(self.db, samples, budget_s * 5 / 3)
        self.fake_route(push_sh=f"  {sampler}")

        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  provider=self.provider())

        seen = [line.split() for line in samples.read_text().splitlines()]
        self.assertGreaterEqual(len(seen), 4, seen)
        self.assertEqual({phase for phase, _ in seen}, {"merge_gate"})
        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl FROM runs"),
            [("awaiting_merge_approval", None, self.URL)])
        return [int(beat) for _, beat in seen], knobs.heartbeat_stale_ms

    def assert_kept_beating(self, beats, stale_ms):
        self.assertGreater(beats[-1], beats[0])
        # No gap between beats reached the stale threshold, give or take a
        # sample and 500 ms of a busy runner's scheduling (KO-674).
        self.assertLess(max(b - a for a, b in zip(beats, beats[1:])),
                        stale_ms + SAMPLE_MS + 500)

    def test_a_slow_push_keeps_the_run_heartbeating(self):
        """The push and the create block for as long as the remote takes,
        outside any agent turn or verify: a push longer than the stale
        budget was a `stale_heartbeat` trip for the supervisor, which could
        fail the run before its URL was recorded. The fake push here samples
        the run's `lastHeartbeat` from the store while it takes longer than
        the whole stale budget; the beat must move under it."""
        self.assert_kept_beating(*self.slow_push())

    def test_a_slow_push_on_a_loaded_runner_keeps_the_run_heartbeating(self):
        self.assert_kept_beating(*self.slow_push(delay_ms=LOADED_MS))

    def test_a_silent_heartbeat_under_a_slow_push_fails_the_check(self):
        beats = self.slow_push(silent=True)
        with self.assertRaises(AssertionError):
            self.assert_kept_beating(*beats)

    def test_a_green_quiet_pr_under_auto_merges_through_the_api(self):
        """Acceptance: zero unresolved threads and green checks with
        `approve = "auto"`: the PR is merged through the merge API -- one
        `PUT .../pulls/7/merge`, never a local merge or a push of main --
        and the run is marked merged with the sha GitHub answered; the
        worktree and local branch are cleaned up, local main untouched."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route()
        provider = self.provider()

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            provider=provider)

        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        self.assertEqual(self.api_calls(),
                         [("state", {"owner": "example", "name": "repo",
                                     "number": 7, "after": None}),
                          # Pinned to the candidate the reviewer approved.
                          ("merge", {"merge_method": "merge",
                                     "commit_title": "feat(x): do y (KO-1) (#7)",
                                     "commit_message": "",
                                     "sha": fake.turns[1].candidate_sha})])
        self.assertIn("gh api --hostname github.com --method PUT"
                      " repos/example/repo/pulls/7/merge --input -",
                      self.recorded())
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [f"git push origin {BRANCH}"])
        # Local main is untouched: the candidate landed on GitHub's main,
        # and the close-out renders no FINDINGS.md by default (KO-363).
        self.assertEqual(self.subjects(), ["base"])
        self.assertNotIn(BRANCH, self.branches())
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha, prUrl FROM runs"),
            [("done", "merged", self.MERGE_SHA, self.URL)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])
        (_, comment) = provider.comments[-1]
        self.assertIn(f"MERGED through {self.URL} as {self.MERGE_SHA}",
                      comment)
        # The persisted ledger line for a pass with nothing to answer opens
        # the way every pass does, so the console reads one shape (KO-373).
        ((ledger,),) = self.read(
            "SELECT text FROM ledger WHERE kind = 'round' AND text LIKE"
            " 'Babysit pass%'")
        self.assertTrue(ledger.startswith(f"Babysit pass 1 over {self.URL}"),
                        ledger)


class MergeAgainstMainTipTests(unittest.TestCase):
    """`_merge_pr()` against a real `origin` whose `main` may be ahead."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.origin = Path(tmp.name) / "origin.git"
        self.wt = Path(tmp.name) / "wt"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin),
                 cwd=tmp.name)
        self.git("clone", "-q", str(self.origin), str(self.wt), cwd=tmp.name)
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "T")
        self.git("checkout", "-qb", "main")
        self.commit("BASE.md")
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-qb", BRANCH)
        self.commit("CANDIDATE.md")
        self.merged = []

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.wt, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, path):
        (self.wt / path).write_text(f"{path}\n")
        self.git("add", path)
        self.git("commit", "-qm", path)
        return self.git("rev-parse", "HEAD")

    def advance_origin_main(self):
        self.git("checkout", "-q", "main")
        self.commit("MOVED.md")
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-q", BRANCH)

    def merge(self, sha=None):
        sha = sha or self.git("rev-parse", "HEAD")
        pull = holophyte.pr.pr_status.parse_pr_url(MergeModeFixture.URL)

        def merge_pull_request(project, pull, pinned):
            self.merged.append(pinned)
            return MergeModeFixture.MERGE_SHA

        with patch.object(holophyte.pr.merge_queue, "merge_queue_required",
                          return_value=False), \
                patch.object(holophyte.pr.github, "merge_pull_request",
                             merge_pull_request), \
                patch("sys.stdout", io.StringIO()):
            return sha, holophyte.pr.pullrequest._merge_pr(
                SimpleNamespace(path=self.wt, config=dict), None, None, None, "KO-1",
                BRANCH, self.wt, sha, 1, pull, retry_conflicts=True)

    def test_a_candidate_behind_origin_main_is_refused_before_github(self):
        self.advance_origin_main()
        self.assert_refused_behind_main()

    def fetch_only_other(self):
        self.git("push", "-q", "origin", "HEAD:refs/heads/other")
        self.git("config", "remote.origin.fetch",
                 "+refs/heads/other:refs/remotes/origin/other")

    def assert_refused_behind_main(self):
        with self.assertRaises(holophyte.pr.github.MergeRefused) as refused:
            self.merge()
        self.assertIn("behind main", str(refused.exception))
        self.assertEqual(self.merged, [])

    def test_a_fetch_refspec_without_main_still_refuses_a_stale_tip(self):
        self.fetch_only_other()
        self.advance_origin_main()
        self.git("update-ref", "refs/remotes/origin/main", "main~1")
        self.assert_refused_behind_main()

    def test_a_fetch_refspec_without_main_still_refuses_a_missing_tip(self):
        self.fetch_only_other()
        self.advance_origin_main()
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        self.assert_refused_behind_main()

    def test_an_origin_without_main_fails_before_github(self):
        self.git("update-ref", "-d", "refs/heads/main", cwd=self.origin)
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        with self.assertRaises(holophyte.loop.gates.InfraFailure):
            self.merge()
        self.assertEqual(self.merged, [])

    def test_a_stalled_main_fetch_fails_within_the_remote_deadline(self):
        self.git("config", "protocol.ext.allow", "always")
        self.git("remote", "set-url", "origin", "ext::sleep 5")
        started = monotonic()
        with patch.object(holophyte.pr.github, "PR_TIMEOUT", 0.5), \
                self.assertRaises(holophyte.loop.gates.InfraFailure) as failed:
            self.merge()
        self.assertLess(monotonic() - started, 4)
        self.assertIn("did not answer", str(failed.exception))
        self.assertEqual(self.merged, [])

    def test_a_candidate_git_cannot_resolve_fails_rather_than_looks_behind(self):
        with self.assertRaises(holophyte.loop.gates.InfraFailure) as failed:
            self.merge(sha="0" * 40)
        self.assertNotIn("behind main", str(failed.exception))
        self.assertEqual(self.merged, [])

    def test_a_candidate_holding_origin_main_merges_through_github(self):
        self.advance_origin_main()
        self.git("merge", "-q", "--no-edit", "main")
        sha, merge_sha = self.merge()
        self.assertEqual(self.merged, [sha])
        self.assertEqual(merge_sha, MergeModeFixture.MERGE_SHA)
