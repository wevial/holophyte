"""KO-712: a pull-request-mode run lands through `main`'s merge queue."""
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


def queue_read(queued=True, merged=False, commit=True):
    """The queue read's answer as GitHub gives it; `commit=False` is a merge
    read before GitHub has named the merge commit."""
    return {"data": {"repository": {"pullRequest": {
        "state": "MERGED" if merged else "OPEN", "merged": merged,
        "mergeCommit": {"oid": QUEUE_SHA} if merged and commit else None,
        "isInMergeQueue": queued}}}}


class MergeQueueTests(MergeModeFixture):
    def land(self, reads, config='[merge]\nmode = "pr"\n'):
        """Run a green, quiet candidate to its landing; the naps taken."""
        self.configure(config)
        self.fake_route(merge_queue=reads)
        naps = []
        with patch.object(holophyte.pr, "SLEEP", naps.append), \
                patch.object(holophyte.merge_queue, "monotonic",
                             side_effect=lambda: sum(naps)):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=self.provider())
        return naps

    def rest_merges(self):
        return [c for c in self.recorded() if "--method PUT" in c]

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


if __name__ == "__main__":
    unittest.main()
