"""A merge-queue ejection under `[merge] queue_full_label`: infra is re-queued
once and parks if it repeats, a park signature parks at once, and a test
failure is reproduced on the pull request by the labelled full run."""
from __future__ import annotations

import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, unquote

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
import holophyte.cli.operator  # noqa: E402 - after the sys.path insert above
import holophyte.deadline  # noqa: E402 - after the sys.path insert above
import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import holophyte.pr.merge_queue  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pr_status  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above

SECOND_GROUP = "8b1c" * 10
CANCELLED = dict(UNIT, conclusion="cancelled")
E2E = dict(UNIT, name="e2e", id=43,
           html_url="https://github.com/example/repo/actions/runs/5/job/43")
LABELLED = '[merge]\nmode = "pr"\nqueue_full_label = "e2e:full"\n'
FULL = (LABELLED + 'queue_full_workflow = "E2E"\n'
        "queue_infra_signatures = ['lost communication with the server']\n")
MERGED_BY_HAND = {"data": {"repository": {"pullRequest": {
    "state": "MERGED", "merged": True, "mergeCommit": {"oid": QUEUE_SHA},
    "mergedBy": {"login": "maintainer"}}}}}
# The latest E2E run on the first head commit, and on the fix commit.
FULL_RUN, FIX_RUN = 77, 78
E2E_HEAD = dict(UNIT, name="e2e", id=44, html_url="https://github.com/example"
                f"/repo/actions/runs/{FULL_RUN}/job/44")
# A failed check left on the head commit by the first attempt of FULL_RUN.
E2E_STALE = dict(E2E_HEAD, id=45, html_url="https://github.com/example"
                 f"/repo/actions/runs/{FULL_RUN}/job/45")
REMOVED_TWICE = [queue_read(), queue_read(queued=False),
                 queue_read(), queue_read(queued=False)]
REQUEUED_AND_MERGED = [queue_read(), queue_read(queued=False),
                       queue_read(), queue_read(queued=False, merged=True)]


class FullWorkflow:
    """The E2E workflow's runs and the pull request's labels, answered for
    `eject`: the first `refuse` re-runs are refused, a red re-run's
    check run reaches the head commit after `late` reads of it, and
    `spent` refuses label removals as a spent call deadline does. The
    first `failing_reads[path]` reads of the labels or the re-run's jobs
    are refused, and `stale` leaves the first attempt's failed check on
    the head commit. The first `failing_reads["add"]` label adds are
    refused together with the labels read straight after each."""

    def __init__(self, test, rerun, abort, labels=(), refuse=0, late=0,
                 spent=False, failing_reads=None, stale=False):
        self.test, self.rerun, self.abort = test, rerun, abort
        self.labels, self.refuse, self.late = set(labels), refuse, late
        self.spent, self.stale = spent, stale
        self.failing = dict(failing_reads or {})
        self.asked, self.reruns = [], []

    def head_checks(self, sha):
        if self.rerun == "success" or not self.reruns \
                or sha != self.test.pushed()[0][1]:
            return []
        self.late -= 1
        return ([E2E_STALE] if self.stale else []) \
            + ([E2E_HEAD] if self.late < 0 else [])

    def refused_read(self, path):
        if self.failing.get(path, 0) > 0:
            self.failing[path] -= 1
            return True
        return False

    def answer(self, args, body):
        label = re.search(r"--method (\w+) \S+/labels(\S*)", args)
        if label and label[1] == "GET":
            if self.refused_read("labels"):
                return refused("gh: Bad Gateway (HTTP 502)")
            return out(json.dumps([{"name": n} for n in sorted(self.labels)]))
        if label and self.relabel(label[1], label[2], body):
            return refused("gh: Bad Gateway (HTTP 502)")
        if "enqueuePullRequest" in (body or ""):
            self.asked.append(("enqueue", None))
        if "actions/runs?head_sha=" in args:
            query = parse_qs(args.split("?", 1)[1].split()[0])
            size, page = int(query["per_page"][0]), int(query["page"][0])
            runs = self.runs_on(query["head_sha"][0])
            return out(json.dumps({"workflow_runs":
                                   runs[(page - 1) * size:page * size]}))
        jobs = re.search(r"actions/runs/(\d+)/attempts/(\d+)/jobs", args)
        if jobs:
            if self.refused_read("jobs"):
                return refused("gh: Bad Gateway (HTTP 502)")
            red = (int(jobs[1]), self.rerun) == (FULL_RUN, "failure")
            return out(json.dumps({"jobs": [{"id": E2E_HEAD["id"],
                                             "conclusion": "failure"}]
                                   if red else []}))
        ran = re.search(r"actions/runs/(\d+)(/\S+)?$", args)
        if ran and ran[2]:
            self.asked.append((ran[2], int(ran[1])))
            if self.refuse:
                self.refuse -= 1
                return refused("gh: Bad Gateway (HTTP 502)")
            self.reruns.append(int(ran[1]))
            return out("{}")
        return out(json.dumps(self.run(int(ran[1])))) if ran else None

    def relabel(self, method, path, body):
        self.asked.append((method, json.loads(body or "null")))
        if method == "POST" and self.refused_read("add"):
            self.failing["labels"] = self.failing.get("labels", 0) + 1
            return True
        if method == "POST":
            self.labels |= set(json.loads(body)["labels"])
            if self.abort == "label":
                self.test.abort_run()
        elif self.spent:
            raise holophyte.deadline.CallRefused("share spent before the call")
        else:
            self.labels.discard(unquote(path.lstrip("/")))

    def runs_on(self, sha):
        """Newest first, as GitHub lists them: a page of newer lint runs
        ahead of the E2E runs."""
        first = sha == self.test.pushed()[0][1]
        ids = (FULL_RUN, 76) if first else (FIX_RUN,)
        lint = [dict(self.run(n), name="lint", head_sha=sha)
                for n in range(300, 200, -1)]
        return lint + ([dict(self.run(95), event="push", head_sha=sha)]
                       if first else []) + [dict(self.run(n), head_sha=sha)
                                            for n in ids]

    def run(self, n):
        done = {"id": n, "name": "E2E", "event": "pull_request",
                "status": "completed", "conclusion": "success",
                "run_attempt": 1}
        if n != FULL_RUN:
            return done
        if not self.reruns:
            return dict(done, conclusion="failure")
        if self.abort is True:
            self.test.abort_run()
            return dict(done, status="in_progress", conclusion=None,
                        run_attempt=2)
        return dict(done, conclusion=self.rerun, run_attempt=2)


class QueueEjectionTests(MergeModeFixture):

    def eject(self, reads, config=LABELLED, log="", annotations=(),
              groups=((GROUP_SHA, CANCELLED),), steps=(), main_moves=False,
              unreadable=(), bodies=None, rerun="success", abort=False,
              group_logs=None, merged_by_hand=False, labels=(), refuse=0,
              late=0, then=None, spent=False, failing_reads=None,
              stale=False):
        """Run a green, quiet candidate into the merge queue; the n-th
        removal reads the n-th of `groups` (the last kept), a merge group
        sha and the check runs it failed with. Every job log reads
        `log`, every check run's annotations `annotations`, served in
        pages of the size asked for; a request whose path holds one of
        `unreadable` fails with an HTTP 502 and one holding a key of
        `bodies` answers its value. `main_moves` lands a commit
        on `origin/main` as the first removal is read. The first pushed
        commit has pull request E2E runs 76 and `FULL_RUN` behind a full
        page of newer lint runs and a newer push run of E2E; a
        re-run of `FULL_RUN` ends `rerun`, and once it ends red the
        head commit reports `E2E_HEAD`. A later commit's E2E run
        `FIX_RUN` is green. `abort` asks the run to abort while the
        re-run is still in progress, or as the label is added when it is
        `"label"`. `self.asked` keeps the label calls,
        re-runs and enqueues in order. `group_logs` maps a merge group
        sha to the job log every check reads once that group is served.
        `merged_by_hand` then runs the loop again with every call readable
        and the pull request read as merged on GitHub by a maintainer.
        The pull request starts with `labels` on, the first `refuse`
        re-runs are refused, and the red re-run's check run reaches the
        head commit only on its `late`-th read after the first; `spent`
        refuses every label removal for a spent call deadline;
        `failing_reads` and `stale` are `FullWorkflow`'s. `then` is called
        last, still against the fake."""
        self.configure(config)
        self.fake_route(merge_queue=reads)
        self.job_log.write_text(log)
        served, routed = list(groups), holophyte.pr.github._gh_run
        reads_of_group = {sha: list(runs) for sha, *runs in groups}

        full = FullWorkflow(self, rerun, abort, labels, refuse, late, spent,
                            failing_reads, stale)
        self.asked = full.asked

        unread, by_hand = list(unreadable), []

        def gh(target, argv, body, path):
            args = " ".join(argv[1:])
            answer = full.answer(args, body)
            if answer is not None:
                return answer
            if by_hand and "mergedBy" in (body or ""):
                return out(json.dumps(MERGED_BY_HAND))
            if any(part in args for part in unread):
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
                      lambda target, pull, sha: reads_of_group.get(sha)
                      or full.head_checks(sha)):
            self.fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                                     Idle(""), *steps, provider=self.provider())
            if merged_by_hand:
                unread.clear()
                by_hand.append(True)
                self.loop(provider=self.provider())
            if then is not None:
                then()

    def hand_off(self):
        """Release the parked run as `holo babysit` does; a successor run
        picks the candidate up on its pull request."""
        holophyte.cli.operator.babysit_ticket(
            self.project, "KO-131", holophyte.cli.operator.BABYSIT_DEFAULT_NOTE,
            out=io.StringIO())
        self.loop(provider=self.provider())

    def abort_run(self):
        conn = store.open(str(self.db))
        try:
            ((run_id,),) = conn.execute("SELECT MAX(id) FROM runs").fetchall()
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

    def test_a_test_failure_labels_the_pull_request_then_reruns_its_latest_pr_full_run(
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

    def test_a_successor_run_reruns_the_refused_full_run_and_unlabels_before_requeue(
            self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), refuse=1, then=self.hand_off)

        self.assertEqual([kind for kind, _ in self.asked],
                         ["enqueue", "POST", "/rerun", "/rerun", "DELETE",
                          "enqueue"])
        self.assertEqual(len(self.read("SELECT summary FROM runEvents WHERE"
                                       " summary LIKE '%did not reproduce%'")),
                         1)
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("abandoned",), ("merged",)])

    def test_a_successor_after_an_unconfirmed_label_add_reruns_the_full_run(
            self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), failing_reads={"add": 1},
                   then=self.hand_off)

        self.assertEqual([kind for kind, _ in self.asked],
                         ["enqueue", "POST", "/rerun", "DELETE", "enqueue"])
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("abandoned",), ("merged",)])

    def test_a_successor_run_that_ends_removes_the_label_its_parked_run_added(
            self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), refuse=1, abort=True,
                   then=self.hand_off)

        self.assertEqual([kind for kind, _ in self.asked],
                         ["enqueue", "POST", "/rerun", "/rerun", "DELETE"])
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("abandoned",), ("abandoned",)])

    def test_a_red_rerun_whose_check_run_reports_late_still_gets_the_fix_round(
            self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), rerun="failure", late=2,
                   steps=(Commit("fix: the e2e failure"), APPROVE, Idle("")))

        (turn,) = [t for t in self.fake.turns if "CHECK e2e" in t.goal]
        self.assertIn(E2E_HEAD["html_url"], turn.goal)

    def test_a_red_rerun_whose_jobs_read_fails_once_fixes_only_its_own_checks(
            self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), rerun="failure", late=2,
                   stale=True, failing_reads={"jobs": 1},
                   steps=(Commit("fix: the e2e failure"), APPROVE, Idle("")))

        (turn,) = [t for t in self.fake.turns if "CHECK e2e" in t.goal]
        self.assertIn(E2E_HEAD["html_url"], turn.goal)
        self.assertNotIn(E2E_STALE["html_url"], turn.goal)

    def test_a_red_rerun_whose_jobs_cannot_be_read_parks_instead_of_fixing(
            self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), rerun="failure", stale=True,
                   failing_reads={"jobs": 10**6})

        self.assertEqual([t for t in self.fake.turns if "CHECK e2e" in t.goal],
                         [])
        self.assertIn("its jobs could not be read", self.question())
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])

    def test_a_red_rerun_with_no_readable_failed_check_parks_naming_the_run(
            self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), rerun="failure", late=10**6)

        question = self.question()
        self.assertIn(f"workflow run {FULL_RUN} attempt 2", question)
        self.assertIn("ended failure", question)
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])

    def test_a_full_suite_label_already_on_is_neither_added_nor_removed(self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), labels=("e2e:full",))

        self.assertEqual([kind for kind, _ in self.asked],
                         ["enqueue", "/rerun", "enqueue"])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", QUEUE_SHA)])

    def test_an_operator_label_behind_a_failed_labels_read_is_never_removed(
            self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), labels=("e2e:full",),
                   failing_reads={"labels": 1}, merged_by_hand=True)

        self.assertNotIn("DELETE", [kind for kind, _ in self.asked])
        self.assertNotIn("POST", [kind for kind, _ in self.asked])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

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

    def test_a_parked_run_with_its_label_on_merged_by_hand_removes_it(self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), unreadable=("/labels/",),
                   merged_by_hand=True)

        self.assertEqual([kind for kind, _ in self.asked],
                         ["enqueue", "POST", "/rerun", "DELETE", "DELETE"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_run_that_ends_with_its_label_on_removes_it(self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), abort=True)

        self.assertEqual([kind for kind, _ in self.asked],
                         ["enqueue", "POST", "/rerun", "DELETE"])
        self.assertEqual(self.read("SELECT outcome FROM runs"),
                         [("abandoned",)])

    def test_a_run_aborted_while_it_adds_its_label_removes_it(self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), abort="label")

        self.assertEqual([kind for kind, _ in self.asked],
                         ["enqueue", "POST", "DELETE"])
        self.assertEqual(self.read("SELECT outcome FROM runs"),
                         [("abandoned",)])

    def test_a_label_removal_refused_at_run_end_by_the_call_deadline_is_warned(self):
        self.eject(REQUEUED_AND_MERGED, config=FULL, log=FAILED,
                   groups=((GROUP_SHA, UNIT),), abort=True,
                   spent=True)

        self.assertEqual(self.read("SELECT outcome FROM runs"),
                         [("abandoned",)])
        self.assertTrue(self.read("SELECT summary FROM runEvents WHERE"
                                  " summary LIKE '%removing e2e:full%failed%'"))

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
