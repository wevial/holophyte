"""`holophyte.babysitter`'s pass under `[merge] mode = "pr"`, end to end."""
from __future__ import annotations

import io
import re
import sys
import unittest
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

import holophyte.agents  # noqa: E402 - after the sys.path insert above
import holophyte.operator  # noqa: E402 - after the sys.path insert above
import holophyte.pr  # noqa: E402 - after the sys.path insert above
import holophyte.pr_status  # noqa: E402 - after the sys.path insert above


class MergeModeBabysitChecksTests(cases.BabysitHelpers, MergeModeFixture):
    """Check folding, parking reasons, and merge vetoes."""
    def test_pending_checks_expire_at_the_configured_deadline(self):
        self.configure('[merge]\nmode = "pr"\ncheck_wait_sec = 60\n')
        self.fake_route(states=[self.pr_state(checks="PENDING")])
        naps = []
        with patch.object(holophyte.pr, "SLEEP", naps.append), \
                patch.object(holophyte.babysitter, "monotonic",
                             side_effect=lambda: sum(naps)):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=self.provider())
        self.assertEqual(sum(naps), 60)
        self.assertIn("pending checks exceeded 60s on the pull request",
                      self.question())
        self.assertEqual({kind for kind, _ in self.api_calls()}, {"state"})


    def test_pending_checks_are_waited_for_before_the_verdict(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state(checks="PENDING"),
                                self.pr_state(checks="SUCCESS")])
        naps = []
        with patch.object(holophyte.pr, "SLEEP", naps.append):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=self.provider())

        self.assertEqual(naps, [holophyte.pr.CHECK_POLL_S])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "state", "merge"])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", self.MERGE_SHA)])


    def test_a_check_runs_read_the_babysitter_cannot_make_is_pending(self):
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


    def test_pr_rounds_caps_the_passes_and_parks_naming_the_cap(self):
        self.configure('[merge]\nmode = "pr"\npr_rounds = 2\n')
        self.fake_route(states=[self.pr_state([self.DEFECT])])
        address = Reply("THREAD 1: ADDRESS -- a real crash")

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            address, Commit("fix 1"), address, Commit("fix 2"),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "implement", "adjudicate",
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


    def test_a_head_that_is_not_the_candidate_parks_instead_of_merging(self):
        self.enterContext(patch.object(holophyte.pr, "SLEEP"))
        self.configure('[merge]\nmode = "pr"\n')
        other = "a" * 40
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


if __name__ == "__main__":
    unittest.main()
