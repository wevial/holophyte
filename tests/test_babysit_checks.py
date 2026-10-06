"""`holophyte.babysit.babysitter`'s pass under `[merge] mode = "pr"`, end to end."""
from __future__ import annotations

import io
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
# Discovery never imports `fake_agent`; put its `tests/` directory on the path.
# Putting it there explicitly makes `discover -s tests` and `-m unittest
# tests.<name>` resolve the harness the same way.
sys.path.insert(0, str(HERE))
import babysit_fixture as cases  # noqa: E402
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    Commit,
    Idle,
    Reply,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    MergeModeFixture,
    StubProvider,
    a_task,
)

import holophyte.agents.roles  # noqa: E402 - after the sys.path insert above
import holophyte.babysit.maintainer_notes  # noqa: E402 - after the sys.path insert above
import holophyte.cli.operator  # noqa: E402 - after the sys.path insert above
import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pr_status  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above


class MergeModeBabysitChecksTests(cases.BabysitHelpers, MergeModeFixture):
    """Check folding, parking reasons, and merge vetoes."""
    def parked_as(self):
        return self.read("SELECT r.phase, r.parkKind, t.activeRunId FROM runs r"
                         " JOIN tickets t ON t.lastRunId = r.id")

    def seen_as(self, state):
        """The park's pull-status read answers `state`'s pull request."""
        real = holophyte.pr.pr_status.graphql
        def graphql(target, pull, query, variables):
            if "mergedBy" not in query:
                return real(target, pull, query, variables)
            return state["data"]
        self.enterContext(patch.object(holophyte.pr.pr_status, "graphql", graphql))

    def test_pending_checks_park_the_run_as_ci_and_free_its_worker(self):
        self.configure('[merge]\nmode = "pr"\n')
        pending = self.pr_state(checks="PENDING")
        self.fake_route(states=[pending, self.pr_state()])
        self.seen_as(pending)
        naps = []
        with patch.object(holophyte.pr.github, "SLEEP", naps.append):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=self.provider())
        self.assertEqual(naps, [])
        self.assertEqual(self.parked_as(),
                         [("awaiting_merge_approval", "ci", None)])
        self.assertEqual(self.read("SELECT prSeenAt, prSeenChecks FROM runs"),
                         [("2000-01-01T00:00:00Z", "pending")])
        self.assertIn("pending checks", self.question())
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])

    def test_a_pause_during_the_pending_read_pauses_instead_of_parking_ci(self):
        self.configure('[merge]\nmode = "pr"\n')
        pending = self.pr_state(checks="PENDING")
        self.fake_route(states=[pending, self.pr_state()])
        self.seen_as(pending)
        real = holophyte.babysit.maintainer_notes.pending_state
        def paused_mid_read(conn, run_id, state, url):
            state = real(conn, run_id, state, url)
            conn = store.open(self.db)
            try:
                store.pause(conn, conn.execute(
                    "SELECT id FROM runs WHERE endedAt IS NULL").fetchone()[0],
                    "reboot writer")
            finally:
                conn.close()
            return state
        with patch.object(holophyte.babysit.maintainer_notes, "pending_state",
                             paused_mid_read), \
                patch.object(holophyte.pr.github, "SLEEP", self.fail):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=self.provider())
        self.assertEqual(self.read("SELECT outcome, resumePhase, parkKind FROM runs"),
                         [("paused", "merge_gate", None)])

    def test_a_green_pr_in_its_quiet_period_parks_as_ci(self):
        self.configure('[merge]\nmode = "pr"\n')
        fresh = datetime.now(timezone.utc).isoformat()
        self.fake_route(states=[self.pr_state(updated_at=fresh)])
        naps = []
        with patch.object(holophyte.pr.github, "SLEEP", naps.append), \
                patch.object(holophyte.babysit.babysitter, "monotonic",
                             side_effect=lambda: sum(naps)):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=self.provider())
        self.assertEqual(naps, [])
        self.assertEqual(self.parked_as(),
                         [("awaiting_merge_approval", "ci", None)])
        self.assertIn("quiet wait", self.question())
        self.assertFalse([v for kind, v in self.api_calls() if kind == "merge"])

    def test_pending_checks_after_a_thread_fix_wait_in_the_worker(self):
        self.configure('[merge]\nmode = "pr"\ncheck_wait_sec = 60\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]),
                                self.pr_state(checks="PENDING")])
        naps = []
        with patch.object(holophyte.pr.github, "SLEEP", naps.append), \
                patch.object(holophyte.babysit.babysitter, "monotonic",
                             side_effect=lambda: sum(naps)):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      Reply("THREAD 1: ADDRESS -- a real crash"),
                      Commit("fix: default load()"), provider=self.provider())
        self.assertEqual(sum(naps), 60)
        self.assertEqual(len(self.pushed()), 2)
        self.assertEqual(self.parked_as(),
                         [("awaiting_merge_approval", "pull_request", None)])
        self.assertIn("pending checks exceeded 60s on the pull request",
                      self.question())

    def test_pending_checks_with_a_required_check_unreported_wait_in_the_worker(self):
        self.configure('[merge]\nmode = "pr"\nmissing_check_sec = 120\n')
        self.fake_route(states=[self.pr_state()])

        def rest(target, pull, method, path, payload=None):
            if "rules/branches/" in path:
                return [{"type": "required_status_checks", "parameters": {
                    "required_status_checks": [{"context": "vercel"}]}}]
            if path.endswith("/branches/main"):
                return {"name": "main", "protected": False}
            return {"total_count": 0, "check_runs": []}
        naps = []
        with patch.object(holophyte.pr.pr_status, "rest", rest), \
                patch.object(holophyte.pr.github, "SLEEP", naps.append), \
                patch.object(holophyte.babysit.babysitter, "monotonic",
                             side_effect=lambda: sum(naps)):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=self.provider())
        self.assertEqual(sum(naps), 120)
        self.assertEqual(self.parked_as(),
                         [("awaiting_merge_approval", "pull_request", None)])
        self.assertIn("required checks never reported on the head commit:"
                      " vercel", self.question())


    def test_a_check_runs_read_the_babysitter_cannot_make_is_pending(self):
        def raising_rest(target, pull, method, path, payload=None):
            raise holophyte.pr.github.InfraFailure(f"GitHub refused GET {path}")
        pull = holophyte.pr.pr_status.parse_pr_url(self.URL)
        with patch.object(holophyte.pr.pr_status, "graphql",
                          lambda *a, **k: self.pr_state(checks="SUCCESS")
                          ["data"]), \
                patch.object(holophyte.pr.pr_status, "rest", raising_rest):
            state = holophyte.pr.pr_status.pr_state(self.project, pull)

        self.assertEqual(state.checks, "pending")
        self.assertEqual(state.head_sha, self.HEAD)


    def test_check_runs_are_read_to_the_last_page_before_green(self):
        """Incomplete pagination cannot establish that every check passed."""
        def success(name):
            return {"name": name, "status": "completed",
                    "conclusion": "success"}
        pages = {}
        calls = []
        def paged_rest(target, pull, method, path, payload=None):
            calls.append(path)
            if "check-runs" not in path:
                return []
            page = int((re.search(r"[&?]page=(\d+)", path) or [0, 1])[1])
            return {"total_count": 101, "check_runs": pages.get(page, [])}

        pages[1] = [success(f"check-{n}") for n in range(100)]
        pages[2] = [{"name": "vitest", "status": "in_progress",
                     "conclusion": None}]
        self.assertEqual(self._state_with_rest(paged_rest).checks, "pending")
        self.assertEqual(
            [c for c in calls if "check-runs" in c],
            [f"repos/example/repo/commits/{self.HEAD}/check-runs?per_page=100",
             f"repos/example/repo/commits/{self.HEAD}/check-runs?per_page=100"
             "&page=2"])

        pages[2] = [success("vitest")]
        self.assertEqual(self._state_with_rest(paged_rest).checks, "success")

        del pages[2]  # 101 promised, 100 delivered: incomplete, pending.
        self.assertEqual(self._state_with_rest(paged_rest).checks, "pending")


    def test_a_check_runs_answer_the_babysitter_cannot_read_is_pending(self):
        """Review finding: `{"check_runs": "unreadable"}` read as green."""
        def odd_rest(target, pull, method, path, payload=None):
            return {"check_runs": "unreadable"} if "check-runs" in path else []
        self.assertEqual(self._state_with_rest(odd_rest).checks, "pending")


    def test_a_rules_answer_the_babysitter_cannot_read_is_pending(self):
        def runs_then(rules):
            def odd_rest(target, pull, method, path, payload=None):
                if "check-runs" in path:
                    return {"total_count": 0, "check_runs": []}
                return rules
            return odd_rest
        rule = {"type": "required_status_checks"}
        for parameters in ("unreadable", None,
                           {"required_status_checks": "unreadable"},
                           {"required_status_checks": ["unreadable"]},
                           {"required_status_checks": [{"context": 7}]}):
            with self.subTest(parameters=parameters):
                rest = runs_then([dict(rule, parameters=parameters)])
                self.assertEqual(self._state_with_rest(rest).checks,
                                 "pending")
        # Other rule types and rules without contexts require nothing.
        rest = runs_then([{"type": "deletion"},
                          dict(rule, parameters={"required_status_checks": []})])
        self.assertEqual(self._state_with_rest(rest).checks, "success")


    def test_a_ticket_edited_during_the_fix_round_is_not_merged(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]),
                                self.pr_state()])
        provider = self.provider()

        class CommitAndEditTheTicket(Commit):
            """The fix commit, with the board edited under it."""

            def play(self, cwd, turn):
                provider.live["iss-131"] = dict(
                    provider.live["iss-131"],
                    title="add a thing, and a second thing")
                return super().play(cwd, turn)

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: ADDRESS -- a real crash"),
                            CommitAndEditTheTicket("fix: default load()"),
                            APPROVE, Idle(""), provider=provider)

        self.assertEqual(fake.roles, ["implement", "review", "implement", "adjudicate",
                                      "implement", "review", "implement"])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "reply", "resolve", "state"])
        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(fake.turns[5].candidate_sha, fixed)
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("failed", "failed", None)])
        self.assertIn(BRANCH, self.branches())
        (_, comment) = provider.comments[-1]
        self.assertIn("MERGE REFUSED", comment)
        self.assertIn("title", comment)
        self.assertIn(fixed, comment)


    def test_babysit_re_entry_runs_the_merge_gate_before_the_api_merge(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.NIT]), self.pr_state()])
        marker = self.worktrees.parent / "verify-must-fail"
        task = dict(a_task(), body=self.BODY, verify=f"test ! -e {marker}")
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  Reply("THREAD 1: DECLINE -- a naming preference"),
                  provider=StubProvider(task))
        approved = self.git("rev-parse", BRANCH).strip()
        for path in self.api_dir.iterdir():
            path.unlink()
        holophyte.cli.operator.babysit_ticket(
            self.project, "KO-131", holophyte.cli.operator.BABYSIT_DEFAULT_NOTE,
            out=io.StringIO())
        marker.write_text("")

        fake, _ = self.loop(provider=StubProvider(task))

        self.assertEqual(fake.roles, [])
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"
                      " WHERE id = 2"),
            [("failed", "failed", None)])
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), approved)
        (_, comment) = self.last_provider.comments[-1]
        self.assertIn("FAILED verify before merge", comment)


    def test_a_head_that_is_not_the_candidate_parks_instead_of_merging(self):
        self.enterContext(patch.object(holophyte.pr.github, "SLEEP"))
        self.configure('[merge]\nmode = "pr"\n')
        other = "a" * 40
        self.enterContext(
            patch("holophyte.pr.pr_head._remote_head", return_value=other)
        )
        self.fake_route(states=[self.pr_state(head=other)])
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"] * 4)
        candidate = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(
            self.read("SELECT phase, outcome, candidateSha FROM runs"),
            [("awaiting_merge_approval", None, candidate)])
        question = self.question()
        self.assertIn(other[:12], question)
        self.assertIn(candidate[:12], question)
        self.assertIn(BRANCH, self.branches())


class MergeModeBabysitCheckFixTests(cases.BabysitHelpers, MergeModeFixture):
    """A red Actions check gets one fix turn per babysit; anything else parks."""
    UNIT = {"name": "unit", "status": "completed", "conclusion": "failure",
            "html_url": "https://github.com/example/repo/actions/runs/5/job/42",
            "id": 42, "app": {"slug": "github-actions"}}
    FAILED = "FAIL: test_x (tests.test_y.Case.test_x)"

    def red_check(self, run, log="", *, until_fixed=False):
        """`run` red on every head, or the first alone; `log` its job's log."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route()
        self.job_log.write_text(log)
        heads, n = [], iter(range(42, 10**4))  # A rerun makes new jobs.
        def check_runs(target, pull, sha):
            heads.extend([sha] if sha not in heads else [])
            green = until_fixed and sha != heads[0]
            return [dict(run, conclusion="success") if green else dict(run, id=next(n))]
        self.enterContext(patch("holophyte.pr.pr_status._check_runs_of", check_runs))

    def assert_parked_on_checks(self):
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])
        self.assertIn("checks failure on the head commit", self.question())

    def test_a_red_actions_check_is_fixed_verified_reviewed_and_merged(self):
        self.red_check(self.UNIT, "".join(f"step {n}\n" for n in range(200))
                       + self.FAILED, until_fixed=True)
        verified = self.worktrees.parent / "verified.log"
        task = dict(a_task(), body=self.BODY,
                    verify=f"git rev-parse HEAD >> {verified}")
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Commit("fix: the unit failure"), APPROVE, Idle(""),
                            provider=StubProvider(task))
        self.assertEqual(fake.roles, ["implement", "review", "implement"] * 2)
        for part in ("CHECK unit", self.UNIT["html_url"], self.FAILED, "step 199"):
            self.assertIn(part, fake.turns[3].goal)
        self.assertNotIn("step 100\n", fake.turns[3].goal)  # The log's tail.
        fixed = self.pushed()[-1][1]
        self.assertIn("fix: the unit", self.git("log", "-1", "--format=%s", fixed))
        self.assertIn(fixed, verified.read_text().split())
        self.assertEqual(fake.turns[4].candidate_sha, fixed)
        self.assertEqual([v["sha"] for kind, v in self.api_calls()
                          if kind == "merge"], [fixed])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_pending_checks_after_a_check_rerun_wait_in_the_worker(self):
        self.configure('[merge]\nmode = "pr"\nbot_authors = ["style-bot"]\n')
        self.fake_route(states=[self.pr_state(), self.pr_state([self.NIT]),
                                self.pr_state(checks="PENDING"), self.pr_state()])
        reads = []
        def check_runs(target, pull, sha):
            reads.append(sha)
            return ([self.UNIT] if len(reads) == 1
                    else [dict(self.UNIT, id=43, conclusion="success")])
        self.enterContext(patch("holophyte.pr.pr_status._check_runs_of", check_runs))
        naps = []
        with patch.object(holophyte.pr.github, "SLEEP", naps.append):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      Reply("THREAD 1: DECLINE -- a naming preference"),
                      provider=self.provider())
        self.assertIn("rerun-failed-jobs", "".join(self.recorded()))
        self.assertEqual(len(self.pushed()), 1)
        self.assertEqual(naps, [holophyte.pr.github.CHECK_POLL_S])
        self.assertEqual(self.read("SELECT outcome, parkKind FROM runs"),
                         [("merged", None)])

    def test_a_check_still_red_after_its_one_fix_parks(self):
        self.red_check(self.UNIT, self.FAILED)
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Commit("fix: the unit failure"), provider=self.provider())
        self.assertEqual(fake.roles, ["implement", "review", "implement", "implement"])
        self.assert_parked_on_checks()

    def test_a_red_status_that_is_not_an_actions_job_parks_unfixed(self):
        # A commit status as `status_contexts_of()` normalises it: no job id.
        self.red_check({"name": "ci/x", "status": "completed", "conclusion": "error"})
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            provider=self.provider())
        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        self.assert_parked_on_checks()

if __name__ == "__main__":
    unittest.main()
