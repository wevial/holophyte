"""A merge-queue ejection under `[merge] queue_full_label`: infra is re-queued
once and parks if it repeats, a park signature parks at once, and a test
failure is reproduced on the pull request by the labelled full run."""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # factory.py imports store by name
sys.path.insert(0, str(HERE))  # discovery never imports `fake_agent`
from fake_agent import APPROVE, Commit, Idle  # noqa: E402
from fake_github import out, refused  # noqa: E402
from loop_fixture import MergeModeFixture  # noqa: E402
from test_merge_queue import (  # noqa: E402
    FAILED,
    GROUP_SHA,
    QUEUE_SHA,
    UNIT,
    group_run,
    queue_read,
)

import holophyte.babysit.queue_eject  # noqa: E402 - after the sys.path insert above
import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import holophyte.pr.merge_queue  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above

SECOND_GROUP = "8b1c" * 10
CANCELLED = dict(UNIT, conclusion="cancelled")
E2E = dict(UNIT, name="e2e", id=43,
           html_url="https://github.com/example/repo/actions/runs/5/job/43")
LABELLED = '[merge]\nmode = "pr"\nqueue_full_label = "e2e:full"\n'
FULL = (LABELLED + 'queue_full_workflow = "E2E"\n'
        "queue_infra_signatures = ['lost communication with the server']\n")
# The latest E2E run on the first head commit, and on the fix commit.
FULL_RUN, FIX_RUN = 77, 78
E2E_HEAD = dict(UNIT, name="e2e", id=44, html_url="https://github.com/example"
                f"/repo/actions/runs/{FULL_RUN}/job/44")
REMOVED_TWICE = [queue_read(), queue_read(queued=False),
                 queue_read(), queue_read(queued=False)]
REQUEUED_AND_MERGED = [queue_read(), queue_read(queued=False),
                       queue_read(), queue_read(queued=False, merged=True)]


class FullWorkflow:
    """The E2E workflow's runs and the label calls, answered for `eject`."""

    def __init__(self, test, rerun, abort):
        self.test, self.rerun, self.abort = test, rerun, abort
        self.asked, self.reruns = [], []

    def answer(self, args, body):
        label = re.search(r"--method (\w+) \S+/labels", args)
        if label:
            self.asked.append((label[1], json.loads(body or "null")))
        if "enqueuePullRequest" in (body or ""):
            self.asked.append(("enqueue", None))
        if "actions/runs?head_sha=" in args:
            sha = args.split("head_sha=", 1)[1].split("&", 1)[0]
            return out(json.dumps({"workflow_runs": self.runs_on(sha)}))
        ran = re.search(r"actions/runs/(\d+)(/\S+)?$", args)
        if ran and ran[2]:
            self.asked.append((ran[2], int(ran[1])))
            self.reruns.append(int(ran[1]))
            return out("{}")
        return out(json.dumps(self.run(int(ran[1])))) if ran else None

    def runs_on(self, sha):
        first = sha == self.test.pushed()[0][1]
        ids = (76, FULL_RUN) if first else (FIX_RUN,)
        return [dict(self.run(n), head_sha=sha) for n in ids] + (
            [dict(self.run(90), name="lint", head_sha=sha)] if first else [])

    def run(self, n):
        done = {"id": n, "name": "E2E", "status": "completed",
                "conclusion": "success", "run_attempt": 1}
        if n != FULL_RUN:
            return done
        if not self.reruns:
            return dict(done, conclusion="failure")
        if self.abort:
            self.test.abort_run()
            return dict(done, status="in_progress", conclusion=None,
                        run_attempt=2)
        return dict(done, conclusion=self.rerun, run_attempt=2)


class QueueEjectionTests(MergeModeFixture):

    def eject(self, reads, config=LABELLED, log="", annotations=(),
              groups=((GROUP_SHA, CANCELLED),), steps=(), main_moves=False,
              unreadable=(), bodies=None, rerun="success", abort=False,
              group_logs=None):
        """Run a green, quiet candidate into the merge queue; the n-th
        removal reads the n-th of `groups` (the last kept), a merge group
        sha and the check runs it failed with. Every job log reads
        `log`, every check run's annotations `annotations`, served in
        pages of the size asked for; a request whose path holds one of
        `unreadable` fails with an HTTP 502 and one holding a key of
        `bodies` answers its value. `main_moves` lands a commit
        on `origin/main` as the first removal is read. The first pushed
        commit has E2E runs 76 and `FULL_RUN` and a newer lint run; a
        re-run of `FULL_RUN` ends `rerun`, and once it ends red the
        head commit reports `E2E_HEAD`. A later commit's E2E run
        `FIX_RUN` is green. `abort` asks the run to abort while the
        re-run is still in progress. `self.asked` keeps the label calls,
        re-runs and enqueues in order. `group_logs` maps a merge group
        sha to the job log every check reads once that group is served."""
        self.configure(config)
        self.fake_route(merge_queue=reads)
        self.job_log.write_text(log)
        served, routed = list(groups), holophyte.pr.github._gh_run
        reads_of_group = {sha: list(runs) for sha, *runs in groups}

        full = FullWorkflow(self, rerun, abort)
        self.asked = full.asked

        def gh(target, argv, body, path):
            args = " ".join(argv[1:])
            answer = full.answer(args, body)
            if answer is not None:
                return answer
            if any(part in args for part in unreadable):
                return refused("gh: Bad Gateway (HTTP 502)")
            body_of = [v for part, v in (bodies or {}).items() if part in args]
            if body_of:
                return out(json.dumps(body_of[0]))
            if "/annotations" in args:
                query = parse_qs(args.split("?", 1)[1].split()[0])
                size, page = int(query["per_page"][0]), int(query.get("page", ["1"])[0])
                return out(json.dumps(
                    list(annotations)[(page - 1) * size:page * size]))
            if "actions/runs?event=merge_group" in args:
                if main_moves and len(served) == len(groups):
                    self.land_on_main()
                sha = (served.pop(0) if len(served) > 1 else served[0])[0]
                if sha in (group_logs or {}):
                    self.job_log.write_text(group_logs[sha])
                return out(json.dumps({"workflow_runs": [
                    dict(group_run("failure"), head_sha=sha)]}))
            return routed(target, argv, body, path)

        naps = []
        with patch.object(holophyte.pr.github, "_gh_run", gh), \
                patch.object(holophyte.pr.github, "SLEEP", naps.append), \
                patch.object(holophyte.pr.merge_queue, "monotonic",
                             side_effect=lambda: sum(naps)), \
                patch.object(holophyte.babysit.queue_eject, "monotonic",
                             side_effect=lambda: sum(naps)), \
                patch("holophyte.pr.pr_status._check_runs_of",
                      lambda target, pull, sha: reads_of_group.get(sha) or (
                          [E2E_HEAD] if rerun != "success" and full.reruns
                          and sha == self.pushed()[0][1] else [])):
            self.fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                                     Idle(""), *steps, provider=self.provider())

    def abort_run(self):
        conn = store.open(str(self.db))
        try:
            ((run_id,),) = conn.execute("SELECT id FROM runs").fetchall()
            store.abort(conn, run_id, "stop here")
        finally:
            conn.close()

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

    def test_a_test_failure_labels_the_pull_request_then_reruns_its_latest_full_run(
            self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),))

        self.assertEqual(self.asked[1:3], [("POST", {"labels": ["e2e:full"]}),
                                           ("/rerun", FULL_RUN)])
        self.assertFalse([c for c in self.recorded()
                          if "dispatches" in c or "workflows/" in c])

    def test_a_reproduced_failure_gets_the_check_fix_round_on_the_head_commit(
            self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), rerun="failure",
                   steps=(Commit("fix: the e2e failure"), APPROVE, Idle("")))

        self.assertEqual(self.fix_turns(), [])
        (turn,) = [t for t in self.fake.turns if "CHECK e2e" in t.goal]
        self.assertIn("failed on the head commit", turn.goal)
        self.assertIn(E2E_HEAD["html_url"], turn.goal)
        self.assertNotIn(GROUP_SHA, turn.goal)

    def test_the_label_comes_off_after_a_green_full_run_before_the_requeue(
            self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), rerun="failure",
                   steps=(Commit("fix: the e2e failure"), APPROVE, Idle("")))

        self.assertEqual([kind for kind, _ in self.asked],
                         ["enqueue", "POST", "/rerun", "DELETE", "enqueue"])
        first, fixed = self.pushed()[0][1], self.pushed()[-1][1]
        self.assertEqual(self.enqueued(), [first, fixed])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", QUEUE_SHA)])

    def test_a_failure_the_full_run_does_not_reproduce_is_unlabelled_and_requeued(
            self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),))

        self.assertEqual(self.fake.roles, ["implement", "review", "implement"])
        self.assertEqual([kind for kind, _ in self.asked],
                         ["enqueue", "POST", "/rerun", "DELETE", "enqueue"])
        ((_, pushed),) = self.pushed()
        self.assertEqual(self.enqueued(), [pushed, pushed])
        (recorded,) = self.read("SELECT summary FROM runEvents WHERE summary"
                                " LIKE '%did not reproduce%'")
        self.assertIn(GROUP_SHA[:12], recorded[0])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", QUEUE_SHA)])

    def test_the_same_spec_ejected_again_with_another_error_parks(self):
        self.eject(REMOVED_TWICE, config=FULL,
                   groups=((GROUP_SHA, UNIT), (SECOND_GROUP, UNIT)),
                   group_logs={GROUP_SHA: f"{FAILED}\nAssertionError: 1 != 2",
                               SECOND_GROUP: f"{FAILED}\nAssertionError:"
                                             " 'a' != 'b'"})

        self.assertEqual([kind for kind, _ in self.asked],
                         ["enqueue", "POST", "/rerun", "DELETE", "enqueue"])
        question = self.question()
        self.assertIn("test_x (tests.test_y.Case.test_x)", question)
        self.assertIn(GROUP_SHA[:12], question)
        self.assertIn(SECOND_GROUP[:12], question)
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])

    def test_a_playwright_spec_renumbered_and_failing_otherwise_parks(self):
        spec = "[chromium] › e2e/login.spec.ts:12:5 › logs in"
        self.eject(REMOVED_TWICE, config=FULL,
                   groups=((GROUP_SHA, UNIT), (SECOND_GROUP, UNIT)),
                   group_logs={
                       GROUP_SHA: f"  1) {spec} ───\n    Error: timed out",
                       SECOND_GROUP: f"  3) {spec} (retry #1) ───\n"
                                     "    Error: expected 2, got 3"})

        self.assertEqual(self.asked.count(("/rerun", FULL_RUN)), 1)
        self.assertIn(spec, self.question())
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])

    def test_a_label_that_cannot_be_removed_parks_instead_of_requeuing(self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), unreadable=("/labels/",))

        self.assertEqual([kind for kind, _ in self.asked],
                         ["enqueue", "POST", "/rerun", "DELETE"])
        self.assertEqual(len(self.enqueued()), 1)
        self.assertIn("removing e2e:full", self.question())
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])

    def test_a_label_that_cannot_be_removed_after_a_fix_is_not_requeued(self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), rerun="failure",
                   unreadable=("/labels/",),
                   steps=(Commit("fix: the e2e failure"), APPROVE, Idle("")))

        self.assertEqual([kind for kind, _ in self.asked],
                         ["enqueue", "POST", "/rerun", "DELETE"])
        self.assertEqual(len(self.enqueued()), 1)
        self.assertIn("removing e2e:full", self.question())

    def test_a_test_failure_with_no_full_run_on_the_head_parks_unlabelled(
            self):
        self.eject(REQUEUED_AND_MERGED,
                   config=FULL.replace('"E2E"', '"E2E (localhost)"'),
                   log=FAILED, groups=((GROUP_SHA, UNIT),))

        self.assertEqual([kind for kind, _ in self.asked], ["enqueue"])
        self.assertIn("queue_full_workflow = 'E2E (localhost)'",
                      self.question())
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])

    def test_a_run_that_ends_with_its_label_on_removes_it(self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), abort=True)

        self.assertEqual([kind for kind, _ in self.asked],
                         ["enqueue", "POST", "/rerun", "DELETE"])
        self.assertEqual(self.read("SELECT outcome FROM runs"),
                         [("abandoned",)])

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

    def test_an_unreadable_job_log_is_requeued_once_then_parks_saying_so(self):
        self.eject(REMOVED_TWICE, log=FAILED,
                   groups=((GROUP_SHA, UNIT), (SECOND_GROUP, UNIT)),
                   unreadable=("jobs/42/logs",))

        self.assertEqual(self.fix_turns(), [])
        self.assertEqual(len(self.enqueued()), 2)
        question = self.question()
        self.assertIn("could not be read", question)
        self.assertIn(GROUP_SHA[:12], question)
        self.assertIn(SECOND_GROUP[:12], question)
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])

    def test_an_annotations_page_failing_after_the_first_is_requeued(self):
        warning = {"annotation_level": "warning",
                   "message": "Node.js 16 actions are deprecated."}
        self.eject(REQUEUED_AND_MERGED, log=FAILED, groups=((GROUP_SHA, UNIT),),
                   annotations=[warning] * 101,
                   unreadable=("annotations?per_page=100&page=2",))

        self.assertEqual(self.fix_turns(), [])
        self.assertEqual(len(self.enqueued()), 2)
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", QUEUE_SHA)])

    def test_an_annotations_page_answering_no_list_is_requeued_not_fixed(
            self):
        warning = {"annotation_level": "warning",
                   "message": "Node.js 16 actions are deprecated."}
        self.eject(REQUEUED_AND_MERGED, log=FAILED, groups=((GROUP_SHA, UNIT),),
                   annotations=[warning] * 101,
                   bodies={"annotations?per_page=100&page=2":
                           {"message": "Server Error"}})

        self.assertEqual(self.fix_turns(), [])
        self.assertEqual(len(self.enqueued()), 2)
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", QUEUE_SHA)])

    def test_a_park_match_in_a_log_whose_annotations_fail_parks_at_once(self):
        line = "Error: secret E2E_TOKEN is not set"
        self.eject(REQUEUED_AND_MERGED,
                   config=LABELLED + "queue_park_signatures ="
                   " ['secret \\S+ is not set']\n",
                   log=f"{line}\n", groups=((GROUP_SHA, UNIT),),
                   unreadable=("/annotations",))

        self.assertEqual(len(self.enqueued()), 1)
        self.assertIn(line, self.question())
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])

    def test_a_park_match_beside_an_unreadable_check_parks_at_once(self):
        line = "Error: secret E2E_TOKEN is not set"
        self.eject(REQUEUED_AND_MERGED,
                   config=LABELLED + "queue_park_signatures ="
                   " ['secret \\S+ is not set']\n",
                   log=f"{line}\n", groups=((GROUP_SHA, UNIT, E2E),),
                   unreadable=("jobs/42/logs",))

        self.assertEqual(len(self.enqueued()), 1)
        self.assertIn(line, self.question())
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])


if __name__ == "__main__":
    unittest.main()
