"""A merge-queue ejection under `[merge] queue_full_label`: infra is re-queued
once and parks if it repeats, a park signature parks at once, and a test
failure keeps the check fix round."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # factory.py imports store by name
sys.path.insert(0, str(HERE))  # discovery never imports `fake_agent`
from fake_agent import APPROVE, Commit, Idle  # noqa: E402
from fake_github import out  # noqa: E402
from loop_fixture import MergeModeFixture  # noqa: E402
from test_merge_queue import (  # noqa: E402
    FAILED,
    GROUP_SHA,
    QUEUE_SHA,
    UNIT,
    group_run,
    queue_read,
)

import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import holophyte.pr.merge_queue  # noqa: E402 - after the sys.path insert above

SECOND_GROUP = "8b1c" * 10
CANCELLED = dict(UNIT, conclusion="cancelled")
LABELLED = '[merge]\nmode = "pr"\nqueue_full_label = "e2e:full"\n'
REMOVED_TWICE = [queue_read(), queue_read(queued=False),
                 queue_read(), queue_read(queued=False)]
REQUEUED_AND_MERGED = [queue_read(), queue_read(queued=False),
                       queue_read(), queue_read(queued=False, merged=True)]


class QueueEjectionTests(MergeModeFixture):
    def eject(self, reads, config=LABELLED, log="", annotations=(),
              groups=((GROUP_SHA, CANCELLED),), steps=(), main_moves=False):
        """Run a green, quiet candidate into the merge queue; the n-th
        removal reads the n-th of `groups` (the last kept), a merge group
        sha and the one check run it failed with. Every job log reads
        `log`, every check run's annotations `annotations`, served in
        pages of the size asked for. `main_moves` lands a commit on
        `origin/main` as the first removal is read."""
        self.configure(config)
        self.fake_route(merge_queue=reads)
        self.job_log.write_text(log)
        served, routed = list(groups), holophyte.pr.github._gh_run
        reads_of_group = {sha: [run] for sha, run in groups}

        def gh(target, argv, body, path):
            args = " ".join(argv[1:])
            if "/annotations" in args:
                query = parse_qs(args.split("?", 1)[1].split()[0])
                size, page = int(query["per_page"][0]), int(query.get("page", ["1"])[0])
                return out(json.dumps(
                    list(annotations)[(page - 1) * size:page * size]))
            if "actions/runs?event=merge_group" in args:
                if main_moves and len(served) == len(groups):
                    self.land_on_main()
                sha, _ = served.pop(0) if len(served) > 1 else served[0]
                return out(json.dumps({"workflow_runs": [
                    dict(group_run("failure"), head_sha=sha)]}))
            return routed(target, argv, body, path)

        naps = []
        with patch.object(holophyte.pr.github, "_gh_run", gh), \
                patch.object(holophyte.pr.github, "SLEEP", naps.append), \
                patch.object(holophyte.pr.merge_queue, "monotonic",
                             side_effect=lambda: sum(naps)), \
                patch("holophyte.pr.pr_status._check_runs_of",
                      lambda target, pull, sha: reads_of_group.get(sha, [])):
            self.fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                                     Idle(""), *steps, provider=self.provider())

    def land_on_main(self):
        moved = self.git("-c", "user.name=Elsewhere",
                         "-c", "user.email=elsewhere@example.invalid",
                         "commit-tree", "main^{tree}", "-p", "main",
                         "-m", "land elsewhere").strip()
        self.git("update-ref", "refs/remotes/origin/main", moved)

    def enqueued(self):
        return [v["sha"] for kind, v in self.api_calls() if kind == "enqueue"]

    def fix_turns(self):
        return [t for t in self.fake.turns if "CHECK unit" in t.goal]

    def test_without_the_label_key_a_cancelled_ejection_gets_the_check_fix_round(
            self):
        self.eject(REQUEUED_AND_MERGED, config='[merge]\nmode = "pr"\n',
                   log=FAILED,
                   steps=(Commit("fix: the unit failure"), APPROVE, Idle("")))

        self.assertEqual(len(self.fix_turns()), 1)
        first, fixed = self.pushed()[0][1], self.pushed()[-1][1]
        self.assertEqual(self.enqueued(), [first, fixed])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", QUEUE_SHA)])

    def test_an_all_cancelled_ejection_is_requeued_once_unlabelled_and_unfixed(
            self):
        self.eject(REQUEUED_AND_MERGED, log="The operation was canceled.")

        self.assertEqual(self.fake.roles, ["implement", "review", "implement"])
        ((_, pushed),) = self.pushed()
        self.assertEqual(self.enqueued(), [pushed, pushed])
        self.assertFalse([c for c in self.recorded() if "/labels" in c])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", QUEUE_SHA)])

    def test_the_same_checks_ejected_again_after_the_requeue_park_naming_both_groups(
            self):
        self.eject(REMOVED_TWICE, groups=((GROUP_SHA, CANCELLED),
                                          (SECOND_GROUP, CANCELLED)))

        self.assertEqual(self.fake.roles, ["implement", "review", "implement"])
        self.assertEqual(len(self.enqueued()), 2)
        question = self.question()
        self.assertIn(GROUP_SHA[:12], question)
        self.assertIn(SECOND_GROUP[:12], question)
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])

    def test_a_failure_whose_log_matches_an_infra_signature_is_requeued(self):
        self.eject(REQUEUED_AND_MERGED,
                   config=LABELLED + "queue_infra_signatures ="
                   " ['lost communication with the server']\n",
                   log="2026-09-23T12:06:00.1Z The self-hosted runner lost"
                       " communication with the server.\n" + FAILED,
                   groups=((GROUP_SHA, UNIT),))

        self.assertEqual(self.fix_turns(), [])
        self.assertEqual(len(self.enqueued()), 2)
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", QUEUE_SHA)])

    def test_the_same_checks_ejected_again_across_a_main_refresh_park_naming_both(
            self):
        self.eject(REMOVED_TWICE + [queue_read(queued=False, merged=True)],
                   groups=((GROUP_SHA, CANCELLED), (SECOND_GROUP, CANCELLED)),
                   main_moves=True)

        before, refreshed = self.enqueued()
        self.assertIn("land elsewhere",
                      self.git("log", "--format=%s", refreshed))
        self.assertNotIn("land elsewhere",
                         self.git("log", "--format=%s", before))
        question = self.question()
        self.assertIn(GROUP_SHA[:12], question)
        self.assertIn(SECOND_GROUP[:12], question)
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])

    def test_an_annotation_whose_raw_details_match_an_infra_signature_is_requeued(
            self):
        self.eject(REQUEUED_AND_MERGED,
                   config=LABELLED + "queue_infra_signatures ="
                   " ['^Runner lost$']\n",
                   log=FAILED, groups=((GROUP_SHA, UNIT),),
                   annotations=[{"annotation_level": "failure",
                                 "message": "Process completed with exit"
                                            " code 1.",
                                 "raw_details": "Context\nRunner lost"}])

        self.assertEqual(self.fix_turns(), [])
        self.assertEqual(len(self.enqueued()), 2)
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", QUEUE_SHA)])

    def test_an_infra_annotation_past_the_first_page_is_requeued(self):
        warning = {"annotation_level": "warning",
                   "message": "Node.js 16 actions are deprecated."}
        self.eject(REQUEUED_AND_MERGED,
                   config=LABELLED + "queue_infra_signatures ="
                   " ['runner has received a shutdown signal']\n",
                   log=FAILED, groups=((GROUP_SHA, UNIT),),
                   annotations=[warning] * 100 + [{
                       "annotation_level": "failure",
                       "message": "The runner has received a shutdown"
                                  " signal."}])

        self.assertEqual(self.fix_turns(), [])
        self.assertEqual(len(self.enqueued()), 2)
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", QUEUE_SHA)])

    def test_a_failing_spec_matching_no_signature_gets_the_check_fix_round(
            self):
        self.eject(REQUEUED_AND_MERGED,
                   config=LABELLED + "queue_infra_signatures ="
                   " ['lost communication with the server']\n",
                   log=FAILED, groups=((GROUP_SHA, UNIT),),
                   steps=(Commit("fix: the unit failure"), APPROVE, Idle("")))

        (turn,) = self.fix_turns()
        self.assertIn(FAILED, turn.goal)
        first, fixed = self.pushed()[0][1], self.pushed()[-1][1]
        self.assertEqual(self.enqueued(), [first, fixed])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", QUEUE_SHA)])

    def test_a_log_matching_a_park_signature_parks_at_once_quoting_the_line(
            self):
        line = "Error: secret E2E_TOKEN is not set"
        self.eject(REQUEUED_AND_MERGED,
                   config=LABELLED + "queue_park_signatures ="
                   " ['secret \\S+ is not set']\n"
                   "queue_infra_signatures = ['.']\n",
                   log=f"2026-09-23T12:06:00Z {line}\n")

        self.assertEqual(self.fake.roles, ["implement", "review", "implement"])
        self.assertEqual(len(self.enqueued()), 1)
        self.assertIn(line, self.question())
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])


if __name__ == "__main__":
    unittest.main()
