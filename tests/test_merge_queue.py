"""KO-712: a pull-request-mode run lands through `main`'s merge queue; KO-714:
a removal for red Actions checks on the merge group gets the one fix turn."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # factory.py imports store by name
sys.path.insert(0, str(HERE))  # discovery never imports `fake_agent`
from fake_agent import APPROVE, Commit, Idle  # noqa: E402
from loop_fixture import MergeModeFixture  # noqa: E402

import holophyte.merge_queue  # noqa: E402 - after the sys.path insert above
import holophyte.pr  # noqa: E402 - after the sys.path insert above

# The queue's merge commit, distinct from the REST merge's `MERGE_SHA`.
QUEUE_SHA = "c0ffee" * 6 + "c0ff"
# The merge-group commit the queue built from main plus the pull request.
GROUP_SHA = "9a0b" * 10
HEAD = MergeModeFixture.HEAD  # The fake serves the branch tip for it.
UNIT = {"name": "unit", "status": "completed", "conclusion": "failure",
        "html_url": "https://github.com/example/repo/actions/runs/5/job/42",
        "id": 42, "app": {"slug": "github-actions"}}
FAILED = "FAIL: test_x (tests.test_y.Case.test_x)"


def queue_read(queued=True, merged=False, commit=True):
    """The queue read's answer as GitHub gives it, a queued entry's
    `headCommit` the pull request's head; `commit=False` is a merge read
    before GitHub has named the merge commit."""
    return {"data": {"repository": {"pullRequest": {
        "state": "MERGED" if merged else "OPEN", "merged": merged,
        "mergeCommit": {"oid": QUEUE_SHA} if merged and commit else None,
        "isInMergeQueue": queued,
        "mergeQueueEntry": {"headCommit": {"oid": HEAD}}
        if queued else None}}}}


def group_run(conclusion, created="2026-09-23T12:05:00Z", number=7):
    """An Actions workflow run of event `merge_group` on pull request
    `number`'s queue branch, at `GROUP_SHA`."""
    return {"event": "merge_group", "head_sha": GROUP_SHA,
            "head_branch": f"gh-readonly-queue/main/pr-{number}-" + "e" * 40,
            "status": "completed", "conclusion": conclusion,
            "created_at": created}


class MergeQueueTests(MergeModeFixture):
    def land(self, reads, config='[merge]\nmode = "pr"\n', steps=(),
             groups=(), group_runs=(), head_runs=()):
        """Run a green, quiet candidate to its landing, the fake agent
        taking `steps` after it, the Actions runs read answering its
        pages `groups` of workflow runs, and `GROUP_SHA` reporting the check runs
        `group_runs`; every other commit reports `head_runs` once queued,
        none before. The naps taken."""
        self.configure(config)
        self.fake_route(merge_queue=reads, merge_groups=groups)
        self.job_log.write_text("".join(f"step {n}\n" for n in range(200))
                                + FAILED)
        naps = []
        with patch.object(holophyte.pr, "SLEEP", naps.append), \
                patch.object(holophyte.merge_queue, "monotonic",
                             side_effect=lambda: sum(naps)), \
                patch("holophyte.pr_status._check_runs_of",
                      lambda target, pull, sha: list(
                          group_runs if sha == GROUP_SHA
                          else head_runs if self.enqueued() else ())):
            self.fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                                     Idle(""), *steps, provider=self.provider())
        return naps

    def rest_merges(self):
        return [c for c in self.recorded() if "--method PUT" in c]

    def enqueued(self):
        return [v["sha"] for kind, v in self.api_calls() if kind == "enqueue"]

    def test_a_queued_candidate_is_enqueued_once_and_lands_as_the_queue_merged_it(
            self):
        self.land([queue_read(), queue_read(queued=False, merged=True)])

        ((_, pushed),) = self.pushed()
        enqueues = [v for kind, v in self.api_calls() if kind == "enqueue"]
        self.assertEqual(enqueues, [{"pull": "PR_1", "sha": pushed}])
        self.assertEqual(self.rest_merges(), [])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", QUEUE_SHA)])

    def test_a_merge_read_before_its_merge_commit_is_named_is_read_again(self):
        naps = self.land([queue_read(queued=False, merged=True, commit=False),
                          queue_read(queued=False, merged=True)])

        self.assertEqual(len(naps), 1)
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", QUEUE_SHA)])

    def test_a_candidate_removed_from_the_queue_parks_on_its_pull_request(self):
        self.land([queue_read(), queue_read(queued=False)])

        self.assertIn("removed from the merge queue", self.question())
        self.assertEqual(self.read("SELECT phase, prUrl FROM runs"),
                         [("awaiting_merge_approval", self.URL)])
        self.assertEqual(self.rest_merges(), [])

    def test_a_candidate_still_queued_past_the_wait_parks_naming_it(self):
        naps = self.land([queue_read()], '[merge]\nmode = "pr"\n'
                                         'check_wait_sec = 60\n')

        self.assertEqual(sum(naps), 60)
        self.assertIn("still in the merge queue after [merge] check_wait_sec"
                      " = 60s", self.question())
        self.assertEqual(self.rest_merges(), [])

    def test_without_a_queue_rule_the_rest_merge_is_sent_and_nothing_enqueued(
            self):
        self.land(None)

        self.assertEqual(len(self.rest_merges()), 1)
        self.assertNotIn("enqueue", [kind for kind, _ in self.api_calls()])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", self.MERGE_SHA)])

    def test_a_removal_red_on_the_merge_group_gets_a_fix_turn_and_requeues(
            self):
        # The head is green; only the merge_group run at GROUP_SHA failed.
        self.land([queue_read(), queue_read(queued=False),
                   queue_read(queued=False, merged=True)],
                  steps=(Commit("fix: the unit failure"), APPROVE, Idle("")),
                  groups=[[group_run("failure")]], group_runs=[UNIT])

        fake = self.fake
        self.assertEqual(fake.roles, ["implement", "review", "implement"] * 2)
        for part in ("CHECK unit", GROUP_SHA, FAILED, "step 199"):
            self.assertIn(part, fake.turns[3].goal)
        self.assertNotIn("head commit", fake.turns[3].goal)
        first, fixed = self.pushed()[0][1], self.pushed()[-1][1]
        self.assertIn("fix: the unit", self.git("log", "-1", "--format=%s",
                                                fixed))
        self.assertEqual(fake.turns[4].candidate_sha, fixed)
        self.assertEqual(self.enqueued(), [first, fixed])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", QUEUE_SHA)])
        self.assertFalse([c for c in self.recorded() if "rerun" in c])

    def test_a_red_merge_group_run_past_the_first_page_still_gets_the_fix(
            self):
        # A hundred newer queue runs of another pull request fill page 1.
        busy = [group_run("success", "2026-09-23T12:09:00Z", number=8)] * 100
        self.land([queue_read(), queue_read(queued=False),
                   queue_read(queued=False, merged=True)],
                  steps=(Commit("fix: the unit failure"), APPROVE, Idle("")),
                  groups=[busy, [group_run("failure")]], group_runs=[UNIT])

        self.assertIn("CHECK unit", self.fake.turns[3].goal)
        self.assertTrue([c for c in self.recorded()
                         if "event=merge_group" in c and c.endswith("page=2")])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", QUEUE_SHA)])

    def test_a_removal_with_a_green_merge_group_parks_unfixed_despite_a_red_head(
            self):
        self.land([queue_read(), queue_read(queued=False)],
                  groups=[[group_run("success")]],
                  group_runs=[dict(UNIT, conclusion="success")],
                  head_runs=[UNIT])

        self.assertEqual(self.fake.roles, ["implement", "review", "implement"])
        self.assertIn("removed from the merge queue", self.question())
        self.assertNotIn("unit", self.question())
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])

    def test_a_red_merge_group_from_before_the_enqueue_parks_unfixed(self):
        self.land([queue_read(), queue_read(queued=False)],
                  groups=[[group_run("failure", "2026-09-23T11:59:59Z")]],
                  group_runs=[UNIT])

        self.assertEqual(self.fake.roles, ["implement", "review", "implement"])
        self.assertIn("removed from the merge queue", self.question())
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])

    def test_a_second_red_removal_after_the_fix_turn_parks_naming_the_checks(
            self):
        self.land([queue_read(), queue_read(queued=False),
                   queue_read(), queue_read(queued=False)],
                  steps=(Commit("fix: the unit failure"), APPROVE, Idle("")),
                  groups=[[group_run("cancelled")]], group_runs=[UNIT])

        self.assertEqual(self.fake.roles,
                         ["implement", "review", "implement"] * 2)
        self.assertEqual(len(self.enqueued()), 2)
        self.assertIn(f"checks unit failed on the merge group {GROUP_SHA[:12]}",
                      self.question())
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])


if __name__ == "__main__":
    unittest.main()
