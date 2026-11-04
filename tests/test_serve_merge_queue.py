"""`GET /merge-queue`: the base branch's merge queue as GitHub holds it, each
building entry's check runs, the pull requests that left it in the last two
hours, and the store run behind each pull request.

GitHub is faked at `github.graphql` and `github.rest` (and the `rest`
`pr_status` imports); `origin` is a real git remote naming a GitHub URL.

Run: python3 -m unittest discover -s tests -p 'test_serve_merge_queue.py' -v
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# `-m unittest tests.<name>` resolves the sibling fixtures as discovery does.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve_fixture import MIN, SEC, ServeTestCase  # noqa: E402

import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pr_status  # noqa: E402 - after the sys.path insert above
import holophyte.serve.serve_merge_queue  # noqa: E402 - after the insert
import store  # noqa: E402 - after the sys.path insert above
import store.read  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from holophyte.config.project import Project  # noqa: E402
from holophyte.loop.gates import InfraFailure  # noqa: E402
from tests.host_fixture import git  # noqa: E402
from tests.phase_fixture import finish_run  # noqa: E402
from tests.test_serve_host import HostServeCase  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "serve" / "merge-queue.json"
NOW = 1_750_000_000_000
HOUR = 60 * MIN
REPO = "https://github.com/example/repo"
GROUP_SHA = "9a0b" * 10
REQUIRED = ("unit", "lint", "types", "build", "docs", "e2e")
OTHER = ("coverage", "bench", "audit", "links", "size", "preview", "smoke")
QUEUED_PULLS = (2547, 2550, 2551, 2552)
HAND_REMOVED, FACTORY_REMOVED, MERGED, LONG_REMOVED = 2560, 2561, 2562, 2563


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def url(number):
    return f"{REPO}/pull/{number}"


def pull(number):
    return {"number": number, "title": f"pull request {number}",
            "url": url(number), "headRefOid": f"{number:040d}",
            "author": {"login": "maintainer"}}


def entry(position, number, state):
    return {"position": position, "state": state,
            "enqueuedAt": iso(NOW - (30 - position) * MIN),
            "estimatedTimeToMerge": 600 * position, "jump": False,
            "solo": position == 3, "pullRequest": pull(number)}


def event(kind, ago, reason=None):
    node = {"__typename": kind, "createdAt": iso(NOW - ago)}
    if kind == "RemovedFromMergeQueueEvent":
        node["reason"] = reason
    return node


def timeline(number, *events):
    return {**pull(number), "updatedAt": iso(NOW - MIN),
            "timelineItems": {"nodes": list(events)}}


def check_run(name, n, done):
    """A check run on the merge group, as `GET commits/SHA/check-runs`
    answers it; the first two workflows' runs carry their check suite."""
    return {"id": 900 + n, "name": name,
            "status": "completed" if done else "in_progress",
            "conclusion": "success" if done else None,
            "started_at": iso(NOW - 19 * MIN),
            "completed_at": iso(NOW - 10 * MIN) if done else None,
            "html_url": f"{REPO}/actions/runs/7/job/{900 + n}",
            "check_suite": {"id": 11 if name in REQUIRED else 12}}


class FakeGithub:
    """The queue read, the merge-group runs read, the check runs and main's
    rules as GitHub answers them for a four-entry queue."""

    def __init__(self):
        self.calls, self.failure = [], None
        done = {"unit", "lint", "types", "build", "coverage"}
        self.check_runs = [check_run(name, n, name in done)
                           for n, name in enumerate(REQUIRED + OTHER)]
        self.queue = {
            "configuration": {"mergeMethod": "SQUASH",
                              "mergingStrategy": "ALLGREEN",
                              "maximumEntriesToBuild": 1,
                              "minimumEntriesToMerge": 1,
                              "checkResponseTimeout": 60},
            "entries": {"nodes": [
                entry(3, 2551, "QUEUED"), entry(1, 2547, "AWAITING_CHECKS"),
                entry(4, 2552, "QUEUED"), entry(2, 2550, "QUEUED")]}}
        self.pulls = [
            timeline(2550, event("AddedToMergeQueueEvent", 28 * MIN)),
            timeline(MERGED, event("AddedToMergeQueueEvent", 90 * MIN),
                     event("MergedEvent", HOUR),
                     event("RemovedFromMergeQueueEvent", HOUR - 2 * SEC,
                           None)),
            timeline(HAND_REMOVED, event("AddedToMergeQueueEvent", 50 * MIN),
                     event("RemovedFromMergeQueueEvent", 30 * MIN, "MANUAL")),
            timeline(LONG_REMOVED, event("AddedToMergeQueueEvent", 4 * HOUR),
                     event("RemovedFromMergeQueueEvent", 3 * HOUR,
                           "FAILED_CHECKS")),
            timeline(FACTORY_REMOVED, event("AddedToMergeQueueEvent", HOUR),
                     event("RemovedFromMergeQueueEvent", 40 * MIN,
                           "FAILED_CHECKS"))]
        self.group_runs = [
            {"name": "ci", "event": "merge_group", "head_sha": GROUP_SHA,
             "head_branch": "gh-readonly-queue/main/pr-2547-" + "e" * 40,
             "check_suite_id": 11, "created_at": iso(NOW - 19 * MIN),
             "run_started_at": iso(NOW - 19 * MIN), "status": "in_progress",
             "conclusion": None},
            {"name": "extras", "event": "merge_group", "head_sha": GROUP_SHA,
             "head_branch": "gh-readonly-queue/main/pr-2547-" + "e" * 40,
             "check_suite_id": 12, "created_at": iso(NOW - 18 * MIN),
             "run_started_at": iso(NOW - 18 * MIN), "status": "in_progress",
             "conclusion": None},
            {"name": "ci", "event": "merge_group", "head_sha": "d" * 40,
             "head_branch": "gh-readonly-queue/main/pr-2550-" + "f" * 40,
             "check_suite_id": 13, "created_at": iso(NOW - 17 * MIN),
             "run_started_at": iso(NOW - 17 * MIN), "status": "completed",
             "conclusion": "cancelled"}]
        self.rules = [
            {"type": "required_status_checks",
             "parameters": {"required_status_checks": [
                 {"context": name} for name in REQUIRED]}},
            {"type": "merge_queue", "parameters": {}}]

    def graphql(self, target, pull, query, variables):
        """`pulls` in pages of 50 and the queue's entries in pages of 100,
        as GitHub's cursors page them."""
        self.calls.append(("graphql", variables))
        if self.failure is not None:
            raise self.failure
        if "entriesAfter" in variables and "after" not in variables:
            return {"repository": {"mergeQueue": {"entries": self.page(
                self.queue["entries"]["nodes"], variables["entriesAfter"],
                100)}}}
        pulls = self.page(self.pulls, variables["after"], 50)
        if variables["after"] is not None:
            return {"repository": {"pullRequests": pulls}}
        queue = self.queue and {**self.queue, "entries": self.page(
            self.queue["entries"]["nodes"], None, 100)}
        return {"repository": {"mergeQueue": queue, "pullRequests": pulls}}

    def page(self, nodes, after, size):
        start = int(after or 0)
        return {"nodes": nodes[start:start + size],
                "pageInfo": {"hasNextPage": start + size < len(nodes),
                             "endCursor": str(start + size)}}

    def rest(self, target, pull, method, path, payload=None):
        self.calls.append(("rest", path))
        if "/actions/runs?event=merge_group" in path:
            return {"workflow_runs": self.group_runs}
        if path.startswith(f"repos/example/repo/commits/{GROUP_SHA}/check-runs"):
            return {"total_count": len(self.check_runs),
                    "check_runs": self.check_runs}
        if path.endswith("/rules/branches/main"):
            return self.rules
        raise AssertionError(f"unexpected read {path}")

    def install(self, case):
        for module in (holophyte.pr.github, holophyte.pr.pr_status):
            case.enterContext(patch.object(module, "rest", self.rest))
        case.enterContext(patch.object(holophyte.pr.github, "graphql",
                                       self.graphql))
        case.clock = [NOW / 1000]
        case.enterContext(patch.object(holophyte.serve.serve_merge_queue,
                                       "time", lambda: case.clock[0]))

    def reads(self):
        return [call for call in self.calls if call[0] == "graphql"]


def link_runs(target, team):
    """Run 1 (KO-7, working) carries position 2's pull request; a new
    KO-8 run, working, carries the factory pull request removed 40
    minutes ago. Answers the KO-8 run's id."""
    git(target, "remote", "add", "origin", REPO + ".git")
    conn = store.open(str(Project.locate(target).store_path))
    try:
        store.set_pull_request(conn, 1, url(2550))
        project = store.tickets.ensure_project(conn, team, target)
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-8", linear_identifier="KO-8",
            title="ticket 8", acceptance_criteria=["Given 8, then it works"],
            verification_commands=["echo ok"], time_box_ms=25 * MIN)
        store.tickets.transition(conn, ticket, "in_flight")
        run = store.claim(conn, project, ticket)
        store.set_phase(conn, run, "working")
        store.set_pull_request(conn, run, url(FACTORY_REMOVED))
        return run
    finally:
        conn.close()


def dump(path):
    conn = store.read.open_readonly(path)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


class MergeQueueRouteCase(ServeTestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        git(self.target, "init", "-q", "-b", "main")
        self.factory_run = link_runs(self.target, "team-1")
        self.github = FakeGithub()
        self.github.install(self)
        self.start()

    def read_queue(self):
        code, _, body = self.request("GET", "/merge-queue")
        self.assertEqual(code, 200, body)
        return body


class MergeQueueTests(MergeQueueRouteCase):
    def test_a_repository_without_a_queue_answers_unconfigured_reading_no_checks(
            self):
        self.github.queue = None
        body = self.read_queue()
        self.assertEqual((body["configured"], body["entries"], body["left"]),
                         (False, [], []))
        self.assertEqual([call for call in self.github.calls
                          if "check-runs" in str(call)], [])

    def test_entries_come_in_position_order_and_only_the_building_one_has_checks(
            self):
        body = self.read_queue()
        self.assertEqual([(e["position"], e["pr"]["number"])
                          for e in body["entries"]],
                         list(enumerate(QUEUED_PULLS, start=1)))
        first, *rest = body["entries"]
        self.assertEqual(first["group_sha"], GROUP_SHA)
        self.assertEqual(first["building_ms"], NOW - 19 * MIN)
        checks = first["checks"]
        self.assertEqual((checks["done"], checks["total"], checks["failed"]),
                         (5, 13, 0))
        self.assertEqual(sorted(run["name"] for run in checks["runs"]
                                if run["required"]), sorted(REQUIRED))
        self.assertEqual({run["workflow"] for run in checks["runs"]},
                         {"ci", "extras"})
        for later in rest:
            with self.subTest(position=later["position"]):
                self.assertIsNone(later["group_sha"])
                self.assertEqual(later["checks"], {"done": 0, "total": 0,
                                                   "failed": 0, "runs": []})
        self.assertEqual(body, json.loads(FIXTURE.read_text()))

    def test_an_entry_names_the_store_run_carrying_its_pull_request(self):
        entries = self.read_queue()["entries"]
        self.assertEqual(entries[1]["run"], {"id": self.run, "ticket": "KO-7",
                                             "phase": "working", "round": 0})
        self.assertIsNone(entries[3]["run"])

    def test_left_lists_the_last_two_hours_newest_first_waiting_on_hand_removals(
            self):
        left = self.read_queue()["left"]
        self.assertEqual([(row["pr"]["number"], row["at_ms"], row["outcome"],
                           row["reason"], row["waits_on_you"]) for row in left],
                         [(HAND_REMOVED, NOW - 30 * MIN, "removed", "MANUAL",
                           True),
                          (FACTORY_REMOVED, NOW - 40 * MIN, "removed",
                           "FAILED_CHECKS", False),
                          (MERGED, NOW - HOUR, "merged", None, False)])
        self.assertEqual(left[1]["run"]["id"], self.factory_run)

    def test_a_hand_merge_after_a_removal_keeps_the_removal(self):
        self.github.pulls.append(timeline(
            2564, event("AddedToMergeQueueEvent", 90 * MIN),
            event("RemovedFromMergeQueueEvent", 80 * MIN, "MANUAL"),
            event("MergedEvent", 20 * MIN)))
        (row,) = [row for row in self.read_queue()["left"]
                  if row["pr"]["number"] == 2564]
        self.assertEqual((row["at_ms"], row["outcome"], row["reason"]),
                         (NOW - 80 * MIN, "removed", "MANUAL"))

    def read_queue_merge(self, number, reason):
        self.github.pulls.append(timeline(
            number, event("AddedToMergeQueueEvent", 50 * MIN),
            event("RemovedFromMergeQueueEvent", 20 * MIN + SEC, reason),
            event("MergedEvent", 20 * MIN)))
        (row,) = [row for row in self.read_queue()["left"]
                  if row["pr"]["number"] == number]
        return (row["at_ms"], row["outcome"], row["reason"],
                row["waits_on_you"])

    def test_a_queue_merge_whose_own_removal_comes_first_reads_merged(self):
        self.assertEqual(self.read_queue_merge(2565, "merged"),
                         (NOW - 20 * MIN, "merged", None, False))

    def test_an_upper_case_merged_removal_before_its_merge_reads_merged(self):
        self.assertEqual(self.read_queue_merge(2569, "MERGED"),
                         (NOW - 20 * MIN, "merged", None, False))

    def test_a_merged_removal_with_no_merge_reads_removed(self):
        self.github.pulls.append(timeline(
            2568, event("AddedToMergeQueueEvent", 50 * MIN),
            event("RemovedFromMergeQueueEvent", 20 * MIN, "merged")))
        (row,) = [row for row in self.read_queue()["left"]
                  if row["pr"]["number"] == 2568]
        self.assertEqual((row["at_ms"], row["outcome"], row["reason"]),
                         (NOW - 20 * MIN, "removed", "merged"))

    def test_a_removed_pull_request_queued_again_has_not_left(self):
        self.github.pulls.append(timeline(
            2567, event("AddedToMergeQueueEvent", 50 * MIN),
            event("RemovedFromMergeQueueEvent", 20 * MIN, "MANUAL"),
            event("AddedToMergeQueueEvent", 10 * MIN)))
        self.assertNotIn(2567, [row["pr"]["number"]
                                for row in self.read_queue()["left"]])

    def test_a_queue_longer_than_one_page_answers_every_entry(self):
        self.github.queue["entries"]["nodes"] += [
            entry(position, 4000 + position, "QUEUED")
            for position in range(5, 102)]
        entries = self.read_queue()["entries"]
        self.assertEqual([e["position"] for e in entries], list(range(1, 102)))

    def test_a_manual_removal_just_before_a_hand_merge_stays_removed(self):
        self.github.pulls.append(timeline(
            2566, event("AddedToMergeQueueEvent", 50 * MIN),
            event("RemovedFromMergeQueueEvent", 20 * MIN + 2 * SEC, "MANUAL"),
            event("MergedEvent", 20 * MIN)))
        (row,) = [row for row in self.read_queue()["left"]
                  if row["pr"]["number"] == 2566]
        self.assertEqual((row["at_ms"], row["outcome"], row["reason"],
                          row["waits_on_you"]),
                         (NOW - 20 * MIN - 2 * SEC, "removed", "MANUAL", True))

    def test_a_departure_behind_fifty_more_recently_updated_pulls_is_listed(
            self):
        self.github.pulls[:0] = [timeline(3000 + n) for n in range(50)]
        left = self.read_queue()["left"]
        self.assertEqual([row["pr"]["number"] for row in left],
                         [HAND_REMOVED, FACTORY_REMOVED, MERGED])
        self.assertEqual(len(self.github.reads()), 2)

    def test_a_removed_pull_request_whose_run_failed_waits_on_you(self):
        conn = store.open(str(self.db))
        try:
            finish_run(conn, self.factory_run, "failed")
        finally:
            conn.close()
        (_, factory, _) = self.read_queue()["left"]
        self.assertEqual((factory["run"]["phase"], factory["waits_on_you"]),
                         ("failed", True))


class MergeQueueCacheTests(MergeQueueRouteCase):
    def test_github_is_read_once_per_fifteen_seconds(self):
        first = self.read_queue()
        self.clock[0] += 5
        self.assertEqual(self.read_queue(), first)
        self.assertEqual(len(self.github.reads()), 1)
        self.clock[0] += 11
        self.read_queue()
        self.assertEqual(len(self.github.reads()), 2)

    def test_another_process_serving_the_project_answers_from_the_same_read(
            self):
        first = self.read_queue()
        script = (
            "import json, sys\n"
            "from unittest.mock import patch\n"
            "import holophyte.pr.github as github\n"
            "import holophyte.serve.serve_merge_queue as route\n"
            "from holophyte.config.project import Project\n"
            "def refuse(*args):\n"
            "    raise AssertionError('GitHub was read again')\n"
            "with patch.object(route, 'time', lambda: float(sys.argv[2])), \\\n"
            "        patch.object(github, 'graphql', refuse):\n"
            "    print(json.dumps(route.merge_queue(\n"
            "        Project.locate(sys.argv[1]))[1]))\n")
        done = subprocess.run(
            [sys.executable, "-c", script, str(self.target),
             str(self.clock[0] + 5)], cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), first)
        self.assertEqual(len(self.github.reads()), 1)

    def test_a_failed_read_answers_its_error_and_writes_nothing(self):
        self.github.failure = InfraFailure(
            "GitHub GraphQL refused: Could not resolve to a Repository")
        before = dump(self.db)
        body = self.read_queue()
        self.assertIsNone(body["configured"])
        self.assertEqual(body["error"], "GitHub GraphQL refused: Could not"
                                        " resolve to a Repository")
        self.assertEqual((body["entries"], body["left"]), ([], []))
        self.assertEqual(dump(self.db), before)


class RunsForPrsTests(MergeQueueRouteCase):
    def test_each_url_answers_its_newest_run_and_a_url_with_none_is_absent(self):
        conn = store.open(str(self.db))
        try:
            finish_run(conn, self.run, "failed")
            ticket = store.read.ticket_by_identifier(conn, "KO-7")
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            newer = store.claim(conn, project, ticket.id)
            store.set_pull_request(conn, newer, url(2550))
        finally:
            conn.close()
        conn = store.read.open_readonly(self.db)
        try:
            runs = store.read.runs_for_prs(
                conn, [url(2550), url(FACTORY_REMOVED), url(2552)])
        finally:
            conn.close()
        self.assertEqual({key: (run.id, run.ticket) for key, run in runs.items()},
                         {url(2550): (newer, "KO-7"),
                          url(FACTORY_REMOVED): (self.factory_run, "KO-8")})


class HostMergeQueueTests(HostServeCase):
    def test_a_registered_project_answers_the_project_daemons_body(self):
        link_runs(self.paths["alpha"], "team-alpha")
        github = FakeGithub()
        github.install(self)
        self.start()
        code, body = self.request("GET", "/projects/alpha/merge-queue")
        self.assertEqual(code, 200, body)
        self.assertEqual(body, json.loads(FIXTURE.read_text()))


if __name__ == "__main__":
    import unittest
    unittest.main()
