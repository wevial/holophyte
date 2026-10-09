"""`GET /runs/N/merge` and `POST /actions/merge`: a run parked for a human's
merge is released through the store's `approve` only when GitHub shows its
pull request approved, green, mergeable, quiet and at the parked head.

GitHub's API is faked at `graphql` and `rest` in `holophyte.pr.pr_status`;
`origin` is a real bare repository read with `git ls-remote`.

Run: python3 -m unittest discover -s tests -p 'test_serve_merge.py' -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# `-m unittest tests.<name>` resolves the sibling fixtures as discovery does.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import loop_fixture  # noqa: E402 - after the sys.path insert above
from serve_fixture import ServeTestCase  # noqa: E402

import holophyte.pr.pr_status  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.read  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from holophyte.config.project import Project  # noqa: E402
from holophyte.loop.gates import InfraFailure  # noqa: E402
from tests.host_fixture import git  # noqa: E402
from tests.phase_fixture import park_run  # noqa: E402
from tests.test_serve_host import (  # noqa: E402
    ALPHA_TOKEN,
    MACHINE,
    HostServeCase,
    bearer,
)

FIXTURE = Path(__file__).parent / "fixtures" / "serve" / "run-merge-ready.json"
BYPASSABLE = FIXTURE.with_name("run-merge-bypassable.json")
TOKEN = "merge-action-token"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
BRANCH = "task/ko-7-ticket-7"
URL = "https://github.com/example/repo/pull/31"
FACT_NAMES = ["parked", "human_approval", "review_approved", "checks_passed",
              "mergeable", "threads_resolved", "head_unchanged"]


def review_rule(ruleset_id, count):
    """A `pull_request` rule as `GET rules/branches/main` answers it."""
    return {"type": "pull_request", "ruleset_source_type": "Repository",
            "ruleset_id": ruleset_id,
            "parameters": {"required_approving_review_count": count,
                           "dismiss_stale_reviews_on_push": False,
                           "require_code_owner_review": False,
                           "require_last_push_approval": False,
                           "required_review_thread_resolution": False,
                           "required_reviewers": [],
                           "allowed_merge_methods": ["merge", "squash"]}}


class FakeGithub:
    """GitHub's API as `pr_state()` reads it, answering one pull request."""

    def __init__(self):
        self.calls = []
        self.review, self.checks, self.mergeable = "APPROVED", "SUCCESS", \
            "MERGEABLE"
        self.threads, self.head, self.unreachable = (), None, False
        self.while_reading = None
        self.rules, self.rulesets = [], {}
        self.later_rules_failure, self.rules_reads = None, 0

    def graphql(self, target, pull, query, variables):
        self.calls.append(("graphql", query))
        if self.while_reading is not None:
            self.while_reading()
        if self.unreachable:
            raise InfraFailure("GitHub did not answer POST graphql:"
                               " <urlopen error [Errno 111] Connection refused>")
        return loop_fixture.MergeModeFixture.pr_state(
            threads=self.threads, checks=self.checks, head=self.head,
            mergeable=self.mergeable, review=self.review)["data"]

    def rest(self, target, pull, method, path, payload=None):
        """`later_rules_failure` is raised by a rules read after the one
        `pr_state()` makes following its check-runs read."""
        self.calls.append(("rest", method, path))
        if "check-runs" in path:
            self.rules_reads = 0
            return {"total_count": 0, "check_runs": []}
        if path.endswith("/rules/branches/main"):
            self.rules_reads += 1
            if self.rules_reads > 1 and self.later_rules_failure:
                raise self.later_rules_failure
            return self.rules
        if "/rulesets/" in path:
            answer = self.rulesets[int(path.rsplit("/", 1)[1])]
            if isinstance(answer, Exception):
                raise answer
            return answer
        return {}

    def install(self, case):
        case.enterContext(patch.object(holophyte.pr.pr_status, "graphql",
                                       self.graphql))
        case.enterContext(patch.object(holophyte.pr.pr_status, "rest",
                                       self.rest))

    def merge_calls(self):
        return [call for call in self.calls
                if "mergePullRequest" in call[-1] or "/merge" in call[-1]]


def dump(path):
    conn = store.read.open_readonly(path)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


class MergeCase(ServeTestCase):
    """KO-7's run 1 on `BRANCH`, pushed to a real bare `origin`."""

    def setUp(self):
        super().setUp()
        self.seed()
        origin = self.root / "origin.git"
        git(self.root, "init", "--bare", "-b", "main", str(origin))
        git(self.target, "init", "-b", "main")
        self.commit("base")
        git(self.target, "remote", "add", "origin", str(origin))
        git(self.target, "push", "origin", "main")
        git(self.target, "checkout", "-b", BRANCH)
        self.sha = self.commit("candidate")
        git(self.target, "push", "origin", BRANCH)
        self.github = FakeGithub()
        self.github.head = self.sha
        self.github.install(self)

    def commit(self, name):
        (self.target / name).write_text(name + "\n")
        git(self.target, "add", name)
        git(self.target, "commit", "-m", name)
        return git(self.target, "rev-parse", "HEAD")

    def start_actions(self, approve="human", actions=True):
        path = self.root / "serve.token"
        path.write_text(TOKEN + "\n")
        path.chmod(0o600)
        self.start(f'[merge]\nmode = "pr"\napprove = "{approve}"\n'
                   f'[serve]\ntoken_file = "{path}"\n'
                   + ("actions = true\n" if actions else ""))

    def park(self, run_id=None, candidate=None, approved=None, pr_url=URL):
        run_id = self.run if run_id is None else run_id
        conn = store.open(str(self.db))
        try:
            ticket = store.read.ticket_by_identifier(conn, "KO-7")
            store.set_branch(conn, run_id, BRANCH)
            store.tickets.transition(conn, ticket.id, "blocked_on_operator")
            park_run(conn, run_id, "awaiting_merge_approval",
                     "green and quiet; awaiting a human's merge",
                     candidate_sha=candidate or self.sha,
                     approved_sha=approved, pr_url=pr_url,
                     park_kind="pull_request", now=self.now)
        finally:
            conn.close()

    def supersede(self):
        """Approve the parked run 1, then park a newer run 2 on the ticket."""
        conn = store.open(str(self.db))
        try:
            ticket = store.read.ticket_by_identifier(conn, "KO-7")
            store.approve(conn, ticket.id, "approved on the writer host")
            store.tickets.transition(conn, ticket.id, "in_flight")
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            newer = store.claim(conn, project, ticket.id, now=self.now)
            store.set_phase(conn, newer, "working", now=self.now)
        finally:
            conn.close()
        self.park(run_id=newer)
        return newer

    def read_merge(self, run_id=None):
        run_id = self.run if run_id is None else run_id
        code, _, body = self.request("GET", f"/runs/{run_id}/merge", BEARER)
        self.assertEqual(code, 200, body)
        return body

    def post_merge(self, **body):
        return self.request("POST", "/actions/merge", BEARER, body=body)

    def rows(self, sql, *params):
        conn = store.read.open_readonly(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def approve_summary(self):
        (summary,) = self.rows(
            "SELECT e.summary FROM interventions i JOIN runEvents e"
            " ON e.runId = i.runId AND e.at = i.at AND e.kind = 'intervention'"
            " WHERE i.runId = ? AND i.action = 'approve'", self.run)
        return summary[0]

    def assert_refused(self, reason, run_id=None, **post):
        """Both routes name `reason`, and the POST writes nothing."""
        run_id = self.run if run_id is None else run_id
        before = dump(self.db)
        body = self.read_merge(run_id)
        self.assertEqual((body["ready"], body["reason"]), (False, reason), body)
        self.assertEqual([fact["name"] for fact in body["facts"]], FACT_NAMES)
        code, _, posted = self.post_merge(run=run_id, **post)
        self.assertEqual((code, posted["ok"], posted["reason"]),
                         (200, False, reason), posted)
        self.assertEqual(dump(self.db), before)
        return body


class ReadyMergeTests(MergeCase):
    def test_a_ready_pull_request_reads_ready_and_is_released_to_the_loop(self):
        self.park()
        self.start_actions()
        body = self.read_merge()
        self.assertEqual((body["ready"], body["reason"]), (True, None), body)
        self.assertEqual([(f["name"], f["ok"]) for f in body["facts"]],
                         [(name, True) for name in FACT_NAMES])
        pinned = json.loads(FIXTURE.read_text())
        self.assertEqual(json.loads(json.dumps(body).replace(
            self.sha, "HEAD_SHA")), pinned)

        code, _, posted = self.post_merge(run=self.run, author="ko",
                                          note="looks right")
        self.assertEqual((code, posted["ok"]), (200, True), posted)
        summary = self.approve_summary()
        self.assertIn(f"ko via the console: merge at head {self.sha}", summary)
        self.assertIn("looks right", summary)
        self.assertEqual(self.rows("SELECT status FROM tickets"), [("ready",)])
        self.assertEqual(self.rows("SELECT resumePhase FROM runs WHERE id = ?",
                                   self.run), [("merge_gate",)])
        self.assertEqual(self.github.merge_calls(), [])

    def test_a_merge_answers_its_approve_row_and_a_refusal_answers_null(self):
        self.park()
        self.start_actions()
        self.github.checks = "PENDING"
        before = dump(self.db)
        code, _, refused = self.post_merge(run=self.run)
        self.assertEqual((code, refused["ok"], refused["recorded"]),
                         (200, False, None), refused)
        self.assertEqual(dump(self.db), before)

        self.github.checks = "SUCCESS"
        code, _, merged = self.post_merge(run=self.run)
        (approve,) = self.rows(
            "SELECT id FROM interventions WHERE action = 'approve'")
        self.assertEqual((code, merged["ok"], merged["recorded"]),
                         (200, True, approve[0]), merged)

    def test_a_run_superseded_during_the_github_read_approves_nothing(self):
        self.park()
        self.start_actions()
        superseded = []
        self.github.while_reading = lambda: superseded or superseded.append(
            self.supersede())
        code, _, posted = self.post_merge(run=self.run)
        self.assertEqual((code, posted["ok"], posted.get("reason")),
                         (200, False, "not_parked"), posted)
        (newer,) = superseded
        self.assertEqual(self.rows(
            "SELECT action FROM interventions WHERE runId = ?", newer), [])
        self.assertEqual(self.rows(
            "SELECT phase, endedAt FROM runs WHERE id = ?", newer),
            [("awaiting_merge_approval", None)])
        self.assertEqual(self.rows("SELECT status FROM tickets"),
                         [("blocked_on_operator",)])


class GithubRefusalTests(MergeCase):
    def setUp(self):
        super().setUp()
        self.park()
        self.start_actions()

    def test_a_review_not_approved_is_refused(self):
        for decision in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
            with self.subTest(decision=decision):
                self.github.review = decision
                self.assert_refused("review_not_approved")

    def test_pending_and_failing_checks_are_refused_by_name(self):
        for rollup, reason in (("PENDING", "checks_pending"),
                               ("FAILURE", "checks_failing")):
            with self.subTest(rollup=rollup):
                self.github.checks = rollup
                self.assert_refused(reason)

    def test_a_conflict_and_an_unknown_mergeable_are_refused_by_name(self):
        for mergeable, reason in (("CONFLICTING", "conflicting"),
                                  ("UNKNOWN", "mergeable_unknown")):
            with self.subTest(mergeable=mergeable):
                self.github.mergeable = mergeable
                self.assert_refused(reason)

    def test_an_unresolved_review_thread_is_refused(self):
        self.github.threads = (("app.py", 3, "reviewer", "rename this"),)
        self.assert_refused("threads_unresolved")

    def test_an_unreachable_github_is_refused_as_unreadable(self):
        self.github.unreachable = True
        self.assert_refused("github_unreadable")


class BypassMergeTests(MergeCase):
    """A `REVIEW_REQUIRED` pull request whose only review-asking ruleset,
    `human-review`, the host's GitHub user may bypass."""

    def setUp(self):
        super().setUp()
        self.bypassable()
        self.park()
        self.start_actions()

    def bypassable(self):
        github = self.github
        github.review, github.checks, github.mergeable = "REVIEW_REQUIRED", \
            "SUCCESS", "MERGEABLE"
        github.threads, github.later_rules_failure = (), None
        github.rules = [
            {"type": "deletion", "ruleset_source_type": "Repository",
             "ruleset_id": 23}, review_rule(23, 0), review_rule(24, 1)]
        github.rulesets = {
            23: {"name": "baseline", "current_user_can_bypass": "never"},
            24: {"name": "human-review",
                 "current_user_can_bypass": "pull_requests_only"}}

    def test_a_bypassable_review_reads_as_its_own_reason_naming_the_ruleset(
            self):
        body = self.read_merge()
        self.assertEqual(json.loads(json.dumps(body).replace(
            self.sha, "HEAD_SHA")), json.loads(BYPASSABLE.read_text()))

    def test_a_bypassable_review_merges_only_with_the_flag_and_records_it(
            self):
        self.assert_refused("review_bypassable")
        code, _, posted = self.post_merge(run=self.run, author="ko",
                                          bypass_review=True)
        self.assertEqual((code, posted["ok"]), (200, True), posted)
        self.assertIn("bypassing the required review", posted["detail"])
        summary = self.approve_summary()
        self.assertIn(f"ko via the console: merge at head {self.sha}", summary)
        self.assertIn("bypassing the required review", summary)
        self.assertIn("human-review", summary)
        self.assertNotIn("review_approved", summary)
        self.assertEqual(self.github.merge_calls(), [])

    def test_an_approved_pull_request_under_bypassable_rules_merges_plainly(
            self):
        self.github.review = "APPROVED"
        body = self.read_merge()
        self.assertEqual(json.loads(json.dumps(body).replace(
            self.sha, "HEAD_SHA")), json.loads(FIXTURE.read_text()))

        code, _, posted = self.post_merge(run=self.run, bypass_review=True)
        self.assertEqual((code, posted["ok"]), (200, True), posted)
        self.assertNotIn("bypass", posted["detail"])
        summary = self.approve_summary()
        self.assertIn(", ".join(FACT_NAMES) + " held", summary)
        self.assertNotIn("bypass", summary)

    def test_a_bypass_that_cannot_be_established_stays_not_approved(self):
        unreadable = InfraFailure("GitHub did not answer GET rulesets")
        for case, change, stopped in (
                ("never", lambda: self.github.rulesets.update(
                    {24: {"name": "human-review",
                          "current_user_can_bypass": "never"}}),
                 "ruleset human-review answers current_user_can_bypass never"),
                ("ruleset unreadable", lambda: self.github.rulesets.update(
                    {24: unreadable}), "ruleset 24 is unreadable"),
                ("rules unreadable", lambda: setattr(
                    self.github, "later_rules_failure", unreadable),
                 "the rules on main are unreadable"),
                ("no bypass answer", lambda: self.github.rulesets.update(
                    {24: {"name": "human-review"}}),
                 "ruleset human-review answers current_user_can_bypass"
                 " nothing"),
                ("a rule with no type", lambda: setattr(
                    self.github, "rules", [*self.github.rules, {
                        "ruleset_id": 25, "parameters": {
                            "required_approving_review_count": 1}}]),
                 "a rule on main cannot be made out"),
                ("no name", lambda: self.github.rulesets.update(
                    {24: {"current_user_can_bypass": "pull_requests_only"}}),
                 "ruleset 24 answers no name"),
                ("an empty name", lambda: self.github.rulesets.update(
                    {24: {"name": "",
                          "current_user_can_bypass": "pull_requests_only"}}),
                 "ruleset 24 answers no name"),
                ("a name that is not a string", lambda:
                    self.github.rulesets.update(
                        {24: {"name": 24, "current_user_can_bypass":
                              "pull_requests_only"}}),
                 "ruleset 24 answers no name")):
            with self.subTest(case=case):
                self.bypassable()
                change()
                body = self.assert_refused("review_not_approved",
                                           bypass_review=True)
                self.assertIn(f"no bypass: {stopped}", body["detail"])

    def test_every_other_gap_blocks_a_bypassable_review_unread(self):
        def push():
            self.commit("pushed after the park")
            git(self.target, "push", "origin", BRANCH)
        for reason, change in (
                ("checks_pending", lambda: setattr(
                    self.github, "checks", "PENDING")),
                ("conflicting", lambda: setattr(
                    self.github, "mergeable", "CONFLICTING")),
                ("threads_unresolved", lambda: setattr(
                    self.github, "threads",
                    (("app.py", 3, "reviewer", "rename this"),))),
                ("review_not_approved", lambda: setattr(
                    self.github, "review", "CHANGES_REQUESTED")),
                ("head_moved", push)):
            with self.subTest(reason=reason):
                self.bypassable()
                change()
                self.github.calls.clear()
                self.assert_refused(reason, bypass_review=True)
                self.assertEqual(self.github.rules_reads, 1)
                self.assertEqual([call for call in self.github.calls
                                  if "/rulesets/" in call[-1]], [])

    def test_a_bypass_flag_that_is_not_a_boolean_is_400_and_asks_nothing(
            self):
        before = dump(self.db)
        for flag in ("yes", 1, None):
            with self.subTest(flag=flag):
                code, _, body = self.post_merge(run=self.run,
                                                bypass_review=flag)
                self.assertEqual(code, 400, body)
                self.assertIn("bypass_review", body["error"])
        self.assertEqual((dump(self.db), self.github.calls), (before, []))


class HeadTests(MergeCase):
    def test_a_push_to_origin_after_the_park_is_a_moved_head(self):
        self.park()
        self.start_actions()
        self.commit("pushed after the park")
        git(self.target, "push", "origin", BRANCH)
        body = self.assert_refused("head_moved")
        self.assertEqual(body["facts"][-1]["ok"], False)

    def test_origin_and_the_pull_request_disagreeing_or_both_moved_is_moved(self):
        self.park()
        self.start_actions()
        self.github.head = "f" * 40
        self.assert_refused("head_moved")
        self.github.head = self.commit("pushed with the pull request")
        git(self.target, "push", "origin", BRANCH)
        self.assert_refused("head_moved")

    def test_origin_and_the_pull_request_at_the_approved_sha_are_unchanged(self):
        approved = self.commit("the reviewed fix")
        git(self.target, "push", "origin", BRANCH)
        self.github.head = approved
        self.park(candidate=self.sha, approved=approved)
        self.start_actions()
        body = self.read_merge()
        self.assertEqual(body["facts"][-1]["name"], "head_unchanged")
        self.assertTrue(body["facts"][-1]["ok"], body)
        self.assertEqual(body["head_sha"], approved)


class UnparkedTests(MergeCase):
    def assert_refused_unread(self, reason, run_id=None):
        body = self.assert_refused(reason, run_id)
        self.assertEqual(self.github.calls, [])
        return body

    def test_a_run_that_is_not_its_tickets_newest_is_not_parked(self):
        self.park()
        self.supersede()
        self.start_actions()
        body = self.assert_refused_unread("not_parked", run_id=self.run)
        self.assertEqual(body["detail"],
                         f"run {self.run} is not KO-7's newest run")

    def test_a_run_parked_with_no_pull_request_is_not_parked(self):
        self.park(pr_url=None)
        self.start_actions()
        self.assert_refused_unread("not_parked")

    def test_auto_approval_is_not_a_humans_merge(self):
        self.park()
        self.start_actions(approve="auto")
        body = self.assert_refused_unread("not_human_approval")
        self.assertEqual([f["ok"] for f in body["facts"]],
                         [True] + [False] * 6)


class GateTests(MergeCase):
    def setUp(self):
        super().setUp()
        self.park()

    def test_without_actions_the_route_is_404_and_writes_nothing(self):
        self.start_actions(actions=False)
        before = dump(self.db)
        code, _, _ = self.post_merge(run=self.run)
        self.assertEqual(code, 404)
        self.assertEqual((dump(self.db), self.github.calls), (before, []))

    def test_without_the_exact_bearer_the_route_is_401_and_writes_nothing(self):
        self.start_actions()
        before = dump(self.db)
        for headers in ({}, {"Authorization": "Bearer not-the-token"}):
            with self.subTest(headers=headers):
                code, _, _ = self.request("POST", "/actions/merge", headers,
                                          body={"run": self.run})
                self.assertEqual(code, 401)
        self.assertEqual((dump(self.db), self.github.calls), (before, []))


class HostMergeTests(HostServeCase):
    def test_a_project_merge_answers_only_to_the_machine_token(self):
        github = FakeGithub()
        github.install(self)
        self.config("alpha", '[serve]\ntoken_file = "{}"\n'.format(
            self.token_file(self.root / "alpha.token", ALPHA_TOKEN)))
        self.host_config(machine_token_file=self.machine(), actions=True)
        self.start()
        path = Project.locate(self.paths["alpha"]).store_path
        before = dump(path)
        for headers, status in ((None, 401), (bearer("not-the-token"), 401),
                                (bearer(ALPHA_TOKEN), 401),
                                (bearer(MACHINE), 200)):
            with self.subTest(headers=headers):
                code, body = self.request(
                    "POST", "/projects/alpha/actions/merge", headers,
                    {"run": 1})
                self.assertEqual(code, status, body)
        self.assertEqual((body["ok"], body["reason"]), (False, "not_parked"))
        self.assertEqual((dump(path), github.calls), (before, []))
