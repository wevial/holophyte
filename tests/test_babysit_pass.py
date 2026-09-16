"""Babysitter checks, threads, conflict recovery, and the pre-merge gate."""
from __future__ import annotations

import io
import json
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
# `fake_agent` is a helper, not a test module: discovery never imports it, and
# how this file is imported decides whether `tests/` is on the path at all.
# Putting it there explicitly makes `discover -s tests` and `-m unittest
# tests.<name>` resolve the harness the same way.
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    REQUEST_CHANGES,
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

import holophyte.agents  # noqa: E402 - after the sys.path insert above
import holophyte.operator  # noqa: E402 - after the sys.path insert above
import holophyte.pr  # noqa: E402 - after the sys.path insert above
import holophyte.pr_status  # noqa: E402 - after the sys.path insert above


class MergeModeBabysitPassTests(MergeModeFixture):
    """The `[merge] mode = "pr"` tests that judge and fix the pull
    request's threads and checks; the open, park, resume and merge are
    `MergeModePullRequestTests` (`test_pullrequest.py`)."""

    def conflict_refusal(self, conflict=False):
        """GitHub refuses the first merge after main moves under the PR."""
        import test_babysitter
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route()
        gh = self.calls.parent / "gh"
        text = gh.read_text()
        answer = f"    echo '{{\"sha\":\"{self.MERGE_SHA}\",\"merged\":true}}'"
        self.refusal = "gh: Pull Request has merge conflicts (HTTP 405)"
        marker = self.calls.parent / "refused"
        gh.write_text(text.replace(answer,
            f'    if [ ! -f "{marker}" ]; then\n'
            f'      touch "{marker}"; echo "{self.refusal}" >&2; exit 1\n'
            f'    fi\n{answer}'))
        path = "tests/test_file_sizes.py" if conflict else "MOVED.md"
        # Move the remote only after the initial candidate was reviewed.
        fixture = self
        class MoveMain:
            role = APPROVE.role
            def play(self, cwd, turn):
                fixture.moved = test_babysitter.ConflictingPullRequestTests.remote_main(
                    fixture, path, "main's line\n")
                return APPROVE.play(cwd, turn)
        return MoveMain()

    @staticmethod
    def ratchet_work():
        work = Commit("branch ratchet", path="tests/test_file_sizes.py",
                      body="branch's line\n")
        class CreateTests:
            role = work.role
            def play(self, cwd, turn):
                (cwd / "tests").mkdir(exist_ok=True)
                return work.play(cwd, turn)
        return CreateTests()

    def assert_conflict_merge_landed(self):
        pushes = self.pushed()
        self.assertEqual(len(pushes), 2)
        original, merged = [sha for _, sha in pushes]
        self.assertEqual(self.git("rev-parse", f"{merged}^1").strip(), original)
        self.assertEqual(self.git("rev-parse", f"{merged}^2").strip(), self.moved)
        calls = [v["sha"] for kind, v in self.api_calls() if kind == "merge"]
        self.assertEqual(calls, [original, merged])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", self.MERGE_SHA)])

    def test_conflict_refusal_merges_main_pushes_and_retries(self):
        review = self.conflict_refusal()
        self.loop(Commit("the scripted work"), review, APPROVE,
                  provider=self.provider())
        self.assert_conflict_merge_landed()

    def test_conflict_refusal_runs_the_implementer_and_continues(self):
        review = self.conflict_refusal(conflict=True)
        path = "tests/test_file_sizes.py"
        fake, _ = self.loop(self.ratchet_work(), review,
                            Commit("Merge main: retain both ratchets", path=path,
                                   body="branch's line\nmain's line\n"), APPROVE,
                            provider=self.provider())
        self.assertEqual(fake.roles, ["implement", "review", "implement", "review"])
        self.assertIn(path, fake.turns[2].goal)
        self.assertIn("mid-merge", fake.turns[2].goal)
        self.assertEqual(fake.turns[2].cwd, self.worktrees / "ko-131-add-a-thing")
        self.assert_conflict_merge_landed()
        merged = self.pushed()[-1][1]
        self.assertEqual(self.git("show", f"{merged}:{path}"),
                         "branch's line\nmain's line\n")

    def test_unresolved_conflict_refusal_parks_with_the_refusal(self):
        review = self.conflict_refusal(conflict=True)
        path = "tests/test_file_sizes.py"
        fake, _ = self.loop(self.ratchet_work(), review, Idle("Cannot resolve"),
                            provider=self.provider())
        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        self.assertIn(path, fake.turns[2].goal)
        self.assertIn(self.refusal, self.question())
        self.assertEqual(len(self.pushed()), 1)
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), self.pushed()[0][1])

    def test_pending_checks_are_waited_for_before_the_verdict(self):
        """A pass with no thread and pending checks reads the PR again
        after `CHECK_POLL_S` rather than judging a rollup that is not in
        yet; green on the second read merges."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state(checks="PENDING"),
                                self.pr_state(checks="SUCCESS")])
        naps = []
        with patch.object(holophyte.pr, "SLEEP", naps.append):
            self.loop(Commit("the scripted work"), APPROVE,
                      provider=self.provider())

        self.assertEqual(naps, [holophyte.pr.CHECK_POLL_S])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "state", "merge"])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", self.MERGE_SHA)])

    def test_a_green_pr_quieter_than_pr_quiet_sec_is_not_merged(self):
        """Fresh green PRs wait; exhausting the round cap parks without merging."""
        self.configure('[merge]\nmode = "pr"\npr_rounds = 1\n')
        fresh = (datetime.now(timezone.utc)
                 - timedelta(seconds=10)).isoformat()
        self.fake_route(states=[self.pr_state(updated_at=fresh)])
        naps = []
        # `CHECK_WAIT_S` shortened so the wait's bound is reached in a few
        # polls; the served `updatedAt` stays fresh, so the pull request
        # never goes quiet and the merge API is never called.
        with patch.object(holophyte.pr, "SLEEP", naps.append), \
                patch.object(holophyte.pr, "CHECK_WAIT_S", 45):
            out = self.main_output(Commit("the scripted work"), APPROVE,
                                   provider=self.provider())

        self.assertTrue(naps)
        self.assertRegex(out, r"green and quiet for \d+s of the 300s"
                              r" required; waiting")
        calls = self.api_calls()
        self.assertGreater(len(calls), 1)
        self.assertEqual({kind for kind, _ in calls}, {"state"})
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])
        self.assertIn("pr_rounds = 1", self.question())

    def test_a_green_pr_quiet_for_pr_quiet_sec_merges(self):
        """Acceptance (KO-429): the same pull request with `updatedAt`
        301 s old under `pr_quiet_sec = 300` merges on the first pass."""
        self.configure('[merge]\nmode = "pr"\n')
        quiet = (datetime.now(timezone.utc)
                 - timedelta(seconds=301)).isoformat()
        self.fake_route(states=[self.pr_state(updated_at=quiet)])
        naps = []
        with patch.object(holophyte.pr, "SLEEP", naps.append):
            self.loop(Commit("the scripted work"), APPROVE,
                      provider=self.provider())

        self.assertEqual(naps, [])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge"])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", self.MERGE_SHA)])

    def test_pr_quiet_sec_zero_merges_a_green_pr_on_the_first_pass(self):
        """Acceptance (KO-429): `pr_quiet_sec = 0` keeps the
        merge-as-soon-as-green the babysitter had -- a pull request whose
        `updatedAt` is this second merges without a wait."""
        self.configure('[merge]\nmode = "pr"\npr_quiet_sec = 0\n')
        self.fake_route(states=[self.pr_state(
            updated_at=datetime.now(timezone.utc).isoformat())])
        naps = []
        with patch.object(holophyte.pr, "SLEEP", naps.append):
            self.loop(Commit("the scripted work"), APPROVE,
                      provider=self.provider())

        self.assertEqual(naps, [])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge"])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", self.MERGE_SHA)])

    def test_a_check_runs_read_the_babysitter_cannot_make_is_pending(self):
        """`pr_state()` reads the head's check runs beside the rollup; a
        read that raises leaves `checks` pending -- never green on a
        rollup alone -- and the exception does not escape the read."""
        def raising_rest(target, pull, method, path, payload=None):
            raise holophyte.pr.InfraFailure(f"GitHub refused GET {path}")
        pull = holophyte.pr_status.parse_pr_url(self.URL)
        with patch.object(holophyte.pr_status, "graphql",
                          lambda *a, **k: self.pr_state(checks="SUCCESS")
                          ["data"]), \
                patch.object(holophyte.pr_status, "rest", raising_rest):
            state = holophyte.pr_status.pr_state(self.tgt, pull)

        self.assertEqual(state.checks, "pending")
        self.assertEqual(state.head_sha, self.HEAD)

    def _state_with_rest(self, rest):
        pull = holophyte.pr_status.parse_pr_url(self.URL)
        with patch.object(holophyte.pr_status, "graphql",
                          lambda *a, **k: self.pr_state(checks="SUCCESS")
                          ["data"]), \
                patch.object(holophyte.pr_status, "rest", rest):
            return holophyte.pr_status.pr_state(self.tgt, pull)

    def test_check_runs_are_read_to_the_last_page_before_green(self):
        """All check-run pages must be read and green before merging."""
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
        """Malformed required-check rules keep the PR pending."""
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
        # A rule of another type, and a rule with no contexts, are not
        # pending: they require nothing.
        rest = runs_then([{"type": "deletion"},
                          dict(rule, parameters={"required_status_checks": []})])
        self.assertEqual(self._state_with_rest(rest).checks, "success")

    def test_a_fix_round_is_reviewed_before_the_pr_is_auto_merged(self):
        """The changed candidate gets an independent review before its API merge."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]),
                                self.pr_state()])

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            Reply("THREAD 1: ADDRESS -- a real crash"),
                            Commit("fix: default load()"), APPROVE,
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "adjudicate",
                                      "implement", "review"])
        merge = [v for kind, v in self.api_calls() if kind == "merge"]
        self.assertEqual(len(merge), 1)
        fixed = merge[0]["sha"]
        self.assertNotEqual(fixed, fake.turns[1].candidate_sha)
        # The second review judged the fix commit itself, against main.
        self.assertEqual(fake.turns[4].candidate_sha, fixed)
        self.assertEqual(fake.turns[4].base_sha, self.base)
        self.assertIn(fake.turns[1].candidate_sha[:12], fake.turns[4].goal)
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "reply", "resolve", "state", "merge"])
        self.assertEqual(
            self.read("SELECT round, verdict, reviewerModel FROM reviewRounds"
                      " ORDER BY round"),
            [(1, "pass", holophyte.agents.agent_route(self.tgt, "review")),
             (2, "changes_requested", "github:review-bot"),
             (3, "pass", "github:ci"),
             (4, "pass", holophyte.agents.agent_route(self.tgt, "review"))])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", self.MERGE_SHA)])

    def test_a_ticket_edited_during_the_fix_round_is_not_merged(self):
        """A fixed candidate still passes the drift gate after its review."""
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

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            Reply("THREAD 1: ADDRESS -- a real crash"),
                            CommitAndEditTheTicket("fix: default load()"),
                            APPROVE, provider=provider)

        self.assertEqual(fake.roles, ["implement", "review", "adjudicate",
                                      "implement", "review"])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "reply", "resolve", "state"])
        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(fake.turns[4].candidate_sha, fixed)
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("failed", "failed", None)])
        self.assertIn(BRANCH, self.branches())
        (_, comment) = provider.comments[-1]
        self.assertIn("MERGE REFUSED", comment)
        self.assertIn("title", comment)
        self.assertIn(fixed, comment)

    def test_a_fix_round_the_reviewer_rejects_parks_instead_of_merging(self):
        """The review of the fix commit asks for changes: nothing is merged
        under `approve = "auto"`, no further fix round runs, and the run
        parks on the PR with the reviewer's findings in the question."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]),
                                self.pr_state()])

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            Reply("THREAD 1: ADDRESS -- a real crash"),
                            Commit("fix: default load()"), REQUEST_CHANGES,
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "adjudicate",
                                      "implement", "review"])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "reply", "resolve", "state"])
        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(
            self.read("SELECT phase, outcome, candidateSha FROM runs"),
            [("awaiting_merge_approval", None, fixed)])
        self.assertEqual(
            self.read("SELECT verdict FROM reviewRounds WHERE round = 4"),
            [("changes_requested",)])
        question = self.question()
        self.assertIn(fixed[:12], question)
        self.assertIn("scripted change is incomplete", question)

    def test_a_rejected_fix_is_reviewed_again_on_babysitter_re_entry(self):
        """A previously rejected fix needs approval when the babysitter resumes."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]),
                                self.pr_state()])
        self.loop(Commit("the scripted work"), APPROVE,
                  Reply("THREAD 1: ADDRESS -- a real crash"),
                  Commit("fix: default load()"), REQUEST_CHANGES,
                  provider=self.provider())
        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(self.read("SELECT approvedSha FROM runs"),
                         [(None,)])
        for path in self.api_dir.iterdir():
            path.unlink()
        holophyte.operator.babysit_ticket(self.tgt, "KO-131", "look again",
                                       out=io.StringIO())

        fake, _ = self.loop(REQUEST_CHANGES, provider=self.provider())

        self.assertEqual(fake.roles, ["review"])
        self.assertEqual(fake.turns[0].candidate_sha, fixed)
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        self.assertEqual(
            self.read("SELECT id, phase, outcome, candidateSha, approvedSha,"
                      " mergeSha FROM runs ORDER BY id"),
            [(1, "failed", "abandoned", fixed, None, None),
             (2, "awaiting_merge_approval", None, fixed, None, None)])
        self.assertIn("scripted change is incomplete", self.question())

    def test_babysit_re_entry_merges_the_approved_sha_without_a_review(self):
        """The counterpart: a run parked on a declined nit with its
        candidate still at the sha the reviewer approved carries that sha
        through `--babysit`, so the resumed pass, green and quiet once the
        nit's author closed it, merges under `approve = "auto"` with no
        second review."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.NIT]), self.pr_state()])
        self.loop(Commit("the scripted work"), APPROVE,
                  Reply("THREAD 1: DECLINE -- a naming preference"),
                  provider=self.provider())
        approved = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(self.read("SELECT candidateSha, approvedSha FROM"
                                   " runs"), [(approved, approved)])
        for path in self.api_dir.iterdir():
            path.unlink()
        holophyte.operator.babysit_ticket(self.tgt, "KO-131", "nit closed",
                                       out=io.StringIO())

        fake, _ = self.loop(provider=self.provider())

        self.assertEqual(fake.roles, [])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge"])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"
                                   " WHERE id = 2"),
                         [("merged", self.MERGE_SHA)])

    def test_babysit_re_entry_runs_the_merge_gate_before_the_api_merge(self):
        """Regression: a resumed, approved PR reached the merge API with
        no verify at all -- the park's verify was a process old, and
        `--approve` or `--babysit` vouches for a judgement, not for the
        tree. The ticket's verify command here passes on the first run
        and is made to fail before the resume: the resumed run stops at
        the merge gate, nothing is merged, and the branch stands."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.NIT]), self.pr_state()])
        marker = self.worktrees.parent / "verify-must-fail"
        task = dict(a_task(), body=self.BODY, verify=f"test ! -e {marker}")
        self.loop(Commit("the scripted work"), APPROVE,
                  Reply("THREAD 1: DECLINE -- a naming preference"),
                  provider=StubProvider(task))
        approved = self.git("rev-parse", BRANCH).strip()
        for path in self.api_dir.iterdir():
            path.unlink()
        holophyte.operator.babysit_ticket(self.tgt, "KO-131", "nit closed",
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

    def test_the_review_of_a_fix_is_held_to_the_criteria(self):
        """Regression: the review of the babysitter's fix commit read only
        its verdict line, so an approval that left a criterion
        unwitnessed merged the fix under `approve = "auto"`. It is now
        the gate a review round is: the criterion's finding turns the
        approval into a `REQUEST_CHANGES`, nothing is merged, and the run
        parks with the unwitnessed criterion in the ticket's question."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]),
                                self.pr_state()])

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            Reply("THREAD 1: ADDRESS -- a real crash"),
                            Commit("fix: default load()"),
                            Reply("CRITERION 1: unwitnessed \u2014 no test"
                                  " covers the fix\nVERDICT: APPROVE"),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "adjudicate",
                                      "implement", "review"])
        self.assertIn("Acceptance criteria, numbered:", fake.turns[4].goal)
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "reply", "resolve", "state"])
        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(
            self.read("SELECT phase, outcome, candidateSha, approvedSha,"
                      " mergeSha FROM runs"),
            [("awaiting_merge_approval", None, fixed, None, None)])
        self.assertEqual(
            self.read("SELECT verdict FROM reviewRounds WHERE round = 4"),
            [("changes_requested",)])
        self.assertIn("CRITERION 1: unwitnessed", self.question())

    def test_a_pass_fixes_the_defect_declines_the_nit_and_parks(self):
        """Acceptance: two unresolved threads, a clear defect and a style
        nit, and green checks. One pass: the adjudicator addresses the one
        and declines the other; the defect gets a fix commit, pushed, a
        reply opening `---- Comment by MODEL ----` and naming the sha, and
        is resolved; the nit gets a decline reply and stays open; the pass
        is a `reviewRounds` row routed `github:LOGIN`; and the run parks
        with the nit listed in the ticket's question."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT, self.NIT])])
        provider = self.provider()
        verdicts = Reply("THREAD 1: ADDRESS -- load() must not return None"
                         " on a missing file\n"
                         "THREAD 2: DECLINE -- a naming preference, not a"
                         " defect")

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, verdicts,
                            Commit("fix: default load() to an empty thing"),
                            provider=provider)

        self.assertEqual(fake.roles,
                         ["implement", "review", "adjudicate", "implement"])
        # The adjudicator judged the candidate as pushed, against main.
        self.assertEqual(fake.turns[2].base_sha, self.base)
        self.assertIn(self.URL, fake.turns[2].goal)
        self.assertIn(self.DEFECT[3], fake.turns[2].goal)
        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertNotEqual(fixed, fake.turns[2].candidate_sha)
        self.assertIn("fix: default load() to an empty thing",
                      self.subjects(BRANCH))
        # Two pushes: the candidate, then the fix.
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [f"git push origin {BRANCH}"] * 2)
        calls = self.api_calls()
        self.assertEqual([kind for kind, _ in calls],
                         ["state", "reply", "resolve", "reply"])
        model = holophyte.agents.agent_route(self.tgt, "adjudicate")
        self.assertEqual(calls[1][1]["thread"], "PRRT_1")
        self.assertTrue(calls[1][1]["body"].startswith(
            f"---- Comment by {model} ----\n"), calls[1][1]["body"])
        self.assertIn(fixed, calls[1][1]["body"])
        self.assertEqual(calls[2][1], {"thread": "PRRT_1"})
        self.assertEqual(calls[3][1]["thread"], "PRRT_2")
        self.assertIn("Declined:", calls[3][1]["body"])
        self.assertIn("naming preference", calls[3][1]["body"])
        self.assertEqual(
            self.read("SELECT round, verdict, reviewerModel FROM reviewRounds"
                      " ORDER BY round"),
            [(1, "pass", holophyte.agents.agent_route(self.tgt, "review")),
             (2, "changes_requested", "github:review-bot+style-bot")])
        # Every reply and resolve is on the run's stream.
        events = [summary for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'pull_request'"
            " ORDER BY seq")]
        self.assertEqual(
            [e.split(" thread ")[0] for e in events if " thread " in e],
            ["replied on", "resolved", "replied on"])
        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl, candidateSha FROM runs"),
            [("awaiting_merge_approval", None, self.URL, fixed)])
        question = self.question()
        self.assertTrue(question.startswith(f"PR open: {self.URL}\n"))
        self.assertIn("1 thread(s) declined", question)
        self.assertIn(self.NIT[3], question)
        self.assertNotIn(self.DEFECT[3], question)

    def test_a_fix_round_that_leaves_edits_is_not_pushed_or_resolved(self):
        """An uncommitted fix fails without pushing or resolving the thread."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT])])
        wt = self.worktrees / "ko-131-add-a-thing"

        class CommitLeavingEdits(Commit):
            def play(self, cwd, turn):
                out = super().play(cwd, turn)
                (cwd / "rest-of-the-fix.py").write_text("not committed\n")
                return out

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            Reply("THREAD 1: ADDRESS -- a real crash"),
                            CommitLeavingEdits("fix: half of it"),
                            provider=self.provider())

        self.assertEqual(fake.roles,
                         ["implement", "review", "adjudicate", "implement"])
        # The candidate's push only; the fix never left the machine.
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [f"git push origin {BRANCH}"])
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        self.assertIn("fix: half of it", self.subjects(BRANCH))
        self.assertTrue((wt / "rest-of-the-fix.py").exists())
        self.assertNotIn("WIP", self.subjects(BRANCH))
        ((outcome, reason),) = self.read(
            "SELECT outcome, outcomeReason FROM runs")
        self.assertEqual(outcome, "failed")
        self.assertIn("uncommitted", reason)

    def test_a_thread_follow_up_reaches_the_adjudicator_and_the_question(self):
        """Adjudication and the parked question include the later human reply."""
        self.configure('[merge]\nmode = "pr"\n')
        follow_up = ("Hold on: do we want load() to default at all? Asking"
                     " before anything is changed here.")
        thread = self.DEFECT + ([("ko", follow_up)],)
        self.fake_route(states=[self.pr_state([thread])])

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            Reply("THREAD 1: HUMAN -- the operator asked"),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "adjudicate"])
        goal = fake.turns[2].goal
        self.assertIn(self.DEFECT[3], goal)
        self.assertIn(follow_up, goal)
        self.assertLess(goal.index(self.DEFECT[3]), goal.index(follow_up))
        self.assertIn("@ko", goal)
        question = self.question()
        self.assertIn(f"> {self.DEFECT[3]}", question)
        self.assertIn(f"> {follow_up}", question)
        self.assertIn("@ko", question)

    def test_a_thread_with_a_second_page_of_comments_is_read_to_the_end(self):
        """Adjudication sees all comment pages, including the latest human reply."""
        self.configure('[merge]\nmode = "pr"\n')
        first_reply = ("the-bot", "Still applies after the rebase.")
        last_word = "Please leave this exactly as it is; I will explain in" \
                    " the ticket."
        self.fake_route(
            states=[self.pr_state([self.NIT])],
            comments=[self.comments_page(1, [("ko", last_word)])])
        state = json.loads((Path(self.calls).parent / "states"
                            / "001.json").read_text())
        thread = state["data"]["repository"]["pullRequest"][
            "reviewThreads"]["nodes"][0]
        thread["comments"]["nodes"].append(
            self.comment("1_1", *first_reply))
        thread["comments"]["pageInfo"] = {"hasNextPage": True,
                                          "endCursor": "k1"}
        (Path(self.calls).parent / "states" / "001.json").write_text(
            json.dumps(state))

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            Reply("THREAD 1: HUMAN -- the operator said so"),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "adjudicate"])
        calls = self.api_calls()
        self.assertEqual([kind for kind, _ in calls],
                         ["state", "comments"])
        self.assertEqual(calls[1][1]["thread"], "PRRT_1")
        self.assertEqual(calls[1][1]["after"], "k1")
        goal = fake.turns[2].goal
        self.assertIn(first_reply[1], goal)
        self.assertIn(last_word, goal)
        self.assertLess(goal.index(first_reply[1]), goal.index(last_word))

    def test_a_human_verdict_posts_nothing_and_parks_with_the_thread(self):
        """HUMAN verdicts park with the thread and post nothing."""
        self.configure('[merge]\nmode = "pr"\n')
        asks = ("src/app.py", 30, "ko",
                "Do we want this to be configurable at all?")
        self.fake_route(states=[self.pr_state([asks])])
        verdict = Reply("THREAD 1: HUMAN -- a question about the approach"
                        " for the operator")

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, verdict,
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "adjudicate"])
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [f"git push origin {BRANCH}"])
        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl FROM runs"),
            [("awaiting_merge_approval", None, self.URL)])
        question = self.question()
        self.assertIn("needs a human's answer", question)
        self.assertIn(f"> {asks[3]}", question)
        self.assertIn("src/app.py:30 by @ko", question)
        self.assertEqual(
            self.read("SELECT verdict, reviewerModel FROM reviewRounds"
                      " WHERE round = 2"),
            [("changes_requested", "github:ko")])

    def test_a_thread_a_person_opened_is_human_before_the_adjudicator(self):
        """Default policy parks on a human thread without adjudicating or replying."""
        self.configure('[merge]\nmode = "pr"\n')
        person = ("src/app.py", 30, ("wevial", "User"),
                  "I would rather this stayed as it was; leaving my reasons"
                  " on the ticket.")
        self.fake_route(states=[self.pr_state([person, self.DEFECT])])
        verdicts = Reply("THREAD 1: ADDRESS -- a real crash\n"
                         "THREAD 2: ADDRESS -- whatever it is, fix it")

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, verdicts,
                            Commit("fix: never reached"),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "adjudicate"])
        goal = fake.turns[2].goal
        self.assertIn(self.DEFECT[3], goal)
        self.assertIn("THREAD 1 -- src/app.py:10 by @review-bot", goal)
        self.assertNotIn(person[3], goal)
        self.assertNotIn("wevial", goal)
        self.assertNotIn("THREAD 2", goal)
        # Nothing posted: no reply, no resolve, no fix pushed.
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [f"git push origin {BRANCH}"])
        ((findings, route),) = self.read(
            "SELECT findings, reviewerModel FROM reviewRounds"
            " WHERE round = 2")
        messages = [f["message"] for f in json.loads(findings)]
        self.assertEqual(len(messages), 2, messages)
        self.assertIn("src/app.py:30 @wevial", messages[0])
        self.assertIn("-- HUMAN: opened by a person", messages[0])
        self.assertIn("src/app.py:10 @review-bot", messages[1])
        self.assertIn("-- ADDRESS: a real crash", messages[1])
        self.assertEqual(route, "github:review-bot+wevial")
        ((ledger,),) = self.read(
            "SELECT text FROM ledger WHERE kind = 'round' AND text LIKE"
            " 'Babysit pass%'")
        self.assertIn("1 opened by a person, HUMAN before the adjudicator",
                      ledger)
        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl FROM runs"),
            [("awaiting_merge_approval", None, self.URL)])
        question = self.question()
        self.assertIn("needs a human's answer", question)
        self.assertIn(f"> {person[3]}", question)
        self.assertIn("src/app.py:30 by @wevial", question)

    def test_under_act_a_person_s_address_is_fixed_replied_and_left_open(self):
        """Act policy fixes a human request and leaves its thread open."""
        self.configure('[merge]\nmode = "pr"\nhuman_threads = "act"\n')
        person = ("src/app.py", 30, ("wevial", "User"),
                  "Rename `thing` to `default_thing` here; the bare name"
                  " shadows the module.")
        self.fake_route(states=[self.pr_state([person])])

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            Reply("THREAD 1: ADDRESS -- rename as asked"),
                            Commit("fix: rename thing to default_thing"),
                            provider=self.provider())

        self.assertEqual(fake.roles,
                         ["implement", "review", "adjudicate", "implement"])
        goal = fake.turns[2].goal
        self.assertIn("THREAD 1 -- src/app.py:30 by @wevial", goal)
        self.assertIn(person[3], goal)
        self.assertIn("opened by a person", goal)
        self.assertIn("Never DECLINE a person's thread", goal)
        # The fix round was given the person's thread.
        self.assertIn(person[3], fake.turns[3].goal)
        self.assertIn("@wevial", fake.turns[3].goal)
        fixed = self.git("rev-parse", BRANCH).strip()
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [f"git push origin {BRANCH}"] * 2)
        calls = self.api_calls()
        self.assertEqual([kind for kind, _ in calls], ["state", "reply"])
        model = holophyte.agents.agent_route(self.tgt, "adjudicate")
        self.assertEqual(calls[1][1]["thread"], "PRRT_1")
        self.assertTrue(calls[1][1]["body"].startswith(
            f"---- Comment by {model} ----\n"), calls[1][1]["body"])
        self.assertIn(fixed, calls[1][1]["body"])
        events = [summary for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'pull_request'"
            " ORDER BY seq")]
        self.assertEqual(
            [e.split(" thread ")[0] for e in events if " thread " in e],
            ["replied on"])
        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl, candidateSha FROM runs"),
            [("awaiting_merge_approval", None, self.URL, fixed)])
        question = self.question()
        self.assertNotIn("needs a human's answer", question)
        self.assertIn("1 person's thread(s) addressed and left open",
                      question)
        self.assertIn("src/app.py:30 (@wevial)", question)

    def test_under_act_a_declined_person_is_human_and_the_bot_is_fixed(self):
        """A declined human request parks; an addressed bot request is fixed."""
        self.configure('[merge]\nmode = "pr"\nhuman_threads = "act"\n')
        person = ("src/app.py", 30, ("wevial", "User"),
                  "Should this be configurable at all? I would leave it.")
        self.fake_route(states=[self.pr_state([person, self.DEFECT])])
        verdicts = Reply("THREAD 1: DECLINE -- a preference, not a defect\n"
                         "THREAD 2: ADDRESS -- a real crash")

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, verdicts,
                            Commit("fix: default load() to an empty thing"),
                            provider=self.provider())

        self.assertEqual(fake.roles,
                         ["implement", "review", "adjudicate", "implement"])
        self.assertIn("THREAD 1 -- src/app.py:30 by @wevial",
                      fake.turns[2].goal)
        self.assertIn("THREAD 2 -- src/app.py:10 by @review-bot",
                      fake.turns[2].goal)
        self.assertNotIn(person[3], fake.turns[3].goal)
        self.assertIn(self.DEFECT[3], fake.turns[3].goal)
        fixed = self.git("rev-parse", BRANCH).strip()
        calls = self.api_calls()
        self.assertEqual([kind for kind, _ in calls],
                         ["state", "reply", "resolve"])
        self.assertEqual(calls[1][1]["thread"], "PRRT_2")
        self.assertIn(fixed, calls[1][1]["body"])
        self.assertEqual(calls[2][1], {"thread": "PRRT_2"})
        ((findings,),) = self.read(
            "SELECT findings FROM reviewRounds WHERE round = 2")
        messages = [f["message"] for f in json.loads(findings)]
        self.assertIn("-- HUMAN: a person's thread the adjudicator would not"
                      " address", messages[0])
        self.assertIn("-- ADDRESS: a real crash", messages[1])
        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl, candidateSha FROM runs"),
            [("awaiting_merge_approval", None, self.URL, fixed)])
        question = self.question()
        self.assertIn("needs a human's answer", question)
        self.assertIn(f"> {person[3]}", question)
        self.assertIn("src/app.py:30 by @wevial", question)
        self.assertNotIn(self.DEFECT[3], question)

    def test_under_act_a_bot_s_human_verdict_still_parks_before_acting(self):
        """Act policy still parks before fixing when a bot verdict needs a human."""
        self.configure('[merge]\nmode = "pr"\nhuman_threads = "act"\n')
        asks = ("src/app.py", 30, "ask-bot",
                "Is this API shape what the operator wants long term?")
        self.fake_route(states=[self.pr_state([asks, self.DEFECT])])
        verdicts = Reply("THREAD 1: HUMAN -- a design question for the"
                         " operator\nTHREAD 2: ADDRESS -- a real crash")

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, verdicts,
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "adjudicate"])
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [f"git push origin {BRANCH}"])
        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl FROM runs"),
            [("awaiting_merge_approval", None, self.URL)])
        question = self.question()
        self.assertIn("needs a human's answer", question)
        self.assertIn(f"> {asks[3]}", question)
        self.assertNotIn(f"> {self.DEFECT[3]}", question)

    def test_pr_rounds_caps_the_passes_and_parks_naming_the_cap(self):
        """Repeated defects exhaust the configured rounds and park naming the cap."""
        self.configure('[merge]\nmode = "pr"\npr_rounds = 2\n')
        self.fake_route(states=[self.pr_state([self.DEFECT])])
        address = Reply("THREAD 1: ADDRESS -- a real crash")

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            address, Commit("fix 1"), address, Commit("fix 2"),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "adjudicate",
                                      "implement", "adjudicate", "implement"])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "reply", "resolve",
                          "state", "reply", "resolve", "state"])
        self.assertEqual(
            self.read("SELECT round, reviewerModel FROM reviewRounds"
                      " WHERE reviewerModel LIKE 'github:%' ORDER BY round"),
            [(2, "github:review-bot"), (3, "github:review-bot")])
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])
        question = self.question()
        self.assertIn("pr_rounds = 2", question)
        self.assertIn(self.DEFECT[3], question)

    def test_threads_past_the_first_page_keep_the_pr_from_reading_quiet(self):
        """A thread on a later page prevents a quiet merge."""
        self.configure('[merge]\nmode = "pr"\n')
        full_page = [self.NIT] * holophyte.pr_status.THREADS_PAGE
        self.fake_route(states=[self.pr_state(resolved=full_page,
                                              next_cursor="c1"),
                                self.pr_state([self.DEFECT])])

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            Reply("THREAD 1: HUMAN -- not mine to answer"),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "adjudicate"])
        calls = self.api_calls()
        self.assertEqual([kind for kind, _ in calls], ["state", "state"])
        self.assertEqual([v["after"] for _, v in calls], [None, "c1"])
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])
        self.assertIn(self.DEFECT[3], self.question())

    def test_a_head_that_is_not_the_candidate_parks_instead_of_merging(self):
        """A concurrent head push parks without adjudication or merge."""
        self.configure('[merge]\nmode = "pr"\n')
        other = "a" * 40
        self.fake_route(states=[self.pr_state(head=other)])

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        candidate = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(
            self.read("SELECT phase, outcome, candidateSha FROM runs"),
            [("awaiting_merge_approval", None, candidate)])
        question = self.question()
        self.assertIn(other[:12], question)
        self.assertIn(candidate[:12], question)
        self.assertIn(BRANCH, self.branches())



if __name__ == "__main__":
    unittest.main()
