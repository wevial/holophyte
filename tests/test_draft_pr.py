"""`[merge] pr_draft`: the factory's pull request opens as a draft and is
marked ready for review once, at the point the babysitter would merge it
or park it ready to merge.

No test reaches GitHub. `MergeModeFixture` runs real `git` with a
recording fake `gh`, so the witnesses are the exact `gh pr create` argv
and the GraphQL bodies the fake records; the token path is witnessed by
the JSON handed to a patched `urlopen`.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit, Idle  # noqa: E402
from loop_fixture import BRANCH, MergeModeFixture  # noqa: E402

import holophyte.cli.operator  # noqa: E402
from holophyte.pr import github  # noqa: E402

AUTO = '[merge]\nmode = "pr"\npr_rounds = 1\npr_quiet_sec = 0\n'
DRAFT = 'pr_draft = true\n'


class DraftPullRequestTests(MergeModeFixture):

    def run_loop(self):
        return self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                         provider=self.provider())

    def kinds(self):
        return [kind for kind, _ in self.api_calls()]

    def ready_events(self):
        return self.read("SELECT summary FROM runEvents WHERE kind = 'pr_ready'")

    def park_reason(self):
        return self.question().split("\n")[1]

    def test_a_draft_project_opens_its_pull_request_with_draft(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n' + DRAFT)
        self.fake_route()
        self.run_loop()
        creates = [c for c in self.recorded() if c.startswith("gh pr create")]
        self.assertEqual(creates, [
            f"gh pr create --repo {self.ORIGIN} --base main --head {BRANCH}"
            " --draft --title KO-131: add a thing --body-file -"])

    def test_the_token_path_sends_draft_only_when_the_key_is_on(self):
        self.git("remote", "add", "origin", self.ORIGIN)
        bindir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, bindir)
        (bindir / "git").symlink_to(shutil.which("git"))
        answer = MagicMock()
        answer.__enter__.return_value.read.return_value = json.dumps(
            {"html_url": self.URL}).encode()
        for config, expected in ((DRAFT, {"draft": True}), ("", {})):
            self.configure('[merge]\nmode = "pr"\n' + config)
            with (self.subTest(config=config),
                  patch.dict(os.environ, {"PATH": str(bindir),
                                          "GH_TOKEN": "t0ken"}),
                  patch.object(github.urllib.request, "urlopen",
                               return_value=answer) as urlopen):
                self.assertEqual(github.create_pull_request(
                    self.project, BRANCH, "KO-1: x", "body"), self.URL)
                request = urlopen.call_args.args[0]
                self.assertEqual(request.full_url,
                                 f"{github.API}/repos/example/repo/pulls")
                self.assertEqual(json.loads(request.data), {
                    "title": "KO-1: x", "body": "body", "head": BRANCH,
                    "base": "main", **expected})

    def test_auto_marks_the_draft_ready_then_merges_on_the_next_read(self):
        self.configure(AUTO + DRAFT)
        self.fake_route(states=[self.pr_state(draft=True), self.pr_state()])
        self.run_loop()
        self.assertEqual(self.kinds(), ["state", "ready", "state", "merge"])
        ready = [v for kind, v in self.api_calls() if kind == "ready"]
        self.assertEqual(ready, [{"pull": self.NODE_ID}])
        ((_, sha),) = self.pushed()
        ((summary,),) = self.ready_events()
        self.assertIn(self.URL, summary)
        self.assertIn(sha, summary)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_human_marks_the_draft_ready_then_parks_ready_to_merge(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       'pr_rounds = 1\npr_quiet_sec = 0\n' + DRAFT)
        self.fake_route(states=[self.pr_state(draft=True), self.pr_state()])
        self.run_loop()
        self.assertEqual(self.kinds(), ["state", "ready", "state"])
        self.assertEqual(self.read("SELECT phase FROM runs"),
                         [("awaiting_merge_approval",)])
        self.assertIn("ready to merge", self.question())

    def test_a_draft_still_draft_after_the_mark_parks_until_marked_by_hand(self):
        self.configure(AUTO + DRAFT)
        self.fake_route(states=[self.pr_state(draft=True)])
        self.run_loop()
        self.assertEqual(self.kinds(), ["state", "ready", "state"])
        self.assertTrue(self.park_reason().startswith(
            "the pull request is a draft"), self.park_reason())
        self.assertEqual(self.read("SELECT phase FROM runs"),
                         [("awaiting_merge_approval",)])

        self.serve(self.pr_state())
        holophyte.cli.operator.babysit_ticket(
            self.project, "KO-131", "sent back to the babysitter", out=io.StringIO())
        self.loop(provider=self.provider())
        self.assertEqual(self.kinds().count("ready"), 1)
        self.assertEqual(self.kinds()[-1], "merge")
        self.assertEqual(len(self.ready_events()), 1)
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id")[-1],
                         ("merged",))

    def test_without_the_key_a_draft_parks_unmarked_and_unmerged(self):
        self.configure(AUTO)
        self.fake_route(states=[self.pr_state(draft=True)])
        self.run_loop()
        self.assertEqual(self.kinds(), ["state"])
        self.assertTrue(self.park_reason().startswith(
            "the pull request is a draft"), self.park_reason())
        self.assertEqual(self.ready_events(), [])

    def test_an_adopted_ready_pull_request_merges_unmarked(self):
        self.configure(AUTO + DRAFT)
        self.fake_route(open_pr=self.URL)
        self.run_loop()
        self.assertFalse(any(c.startswith("gh pr create")
                             for c in self.recorded()))
        self.assertEqual(self.kinds(), ["state", "merge"])
        self.assertEqual(self.ready_events(), [])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])


if __name__ == "__main__":
    unittest.main()
