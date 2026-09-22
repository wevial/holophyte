"""`holophyte.babysitter`'s pass under `[merge] mode = "pr"`, end to end."""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
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
    REQUEST_CHANGES,
    Commit,
    Idle,
    Reply,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    MergeModeFixture,
)

import holophyte.agents  # noqa: E402 - after the sys.path insert above
import holophyte.operator  # noqa: E402 - after the sys.path insert above
import holophyte.pr  # noqa: E402 - after the sys.path insert above
import holophyte.pr_status  # noqa: E402 - after the sys.path insert above


class MergeModeBabysitPassTests(cases.ConflictRefusalCases, MergeModeFixture):
    """Pass structure, settling, quiet clocks, and refreshing main."""
    def steps(self):
        return [row[0] for row in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'babysit_step' ORDER BY seq")]

    def test_wait_threads_fix_steps_are_recorded_once_per_change(self):
        self.configure('[merge]\nmode = "pr"\npr_rounds = 1\n')
        fresh = datetime.now(timezone.utc).isoformat()
        self.fake_route(states=[self.pr_state(checks="PENDING"),
                               self.pr_state(checks="PENDING"),
                               self.pr_state(updated_at=fresh),
                               self.pr_state(updated_at=fresh),
                               self.pr_state([self.DEFECT, self.NIT]), self.pr_state()])
        with patch.object(holophyte.pr, "SLEEP"):
            self.loop(Commit("candidate"), APPROVE, Idle(""),
                      Reply("THREAD 1: ADDRESS -- crash\n"
                            "THREAD 2: DECLINE -- preference"),
                      Commit("fix crash"), provider=self.provider())
        self.assertEqual(self.steps(), ["checks", "quiet", "threads", "fix", "parked"])
        self.assertEqual(len(self.pushed()), 2)

    def test_conflict_covering_review_and_park_steps(self):
        review = self.conflict_refusal(conflict=True)
        self.loop(self.ratchet_work(), review, Idle(""),
                  Commit("Resolve main", path="tests/test_file_sizes.py",
                         body="branch's line\nmain's line\n"), REQUEST_CHANGES,
                  provider=self.provider())
        self.assertEqual(self.steps(), ["conflict_merge", "covering_review", "parked"])
        events = self.read("SELECT kind, summary FROM runEvents ORDER BY seq")
        covering = events.index(("babysit_step", "covering_review"))
        self.assertEqual(events[covering - 1][0], "phase_change")
        self.assertIn("-> reviewing: review of the fix at", events[covering - 1][1])

    def test_approved_fix_review_carries_unchanged_witness(self):
        self.covering_approval(False)

    def test_approved_fix_review_rejects_changed_witness(self):
        self.covering_approval(True)

    def covering_approval(self, touch_test):
        self.configure('[merge]\nmode = "pr"\npr_quiet_sec = 0\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]), self.pr_state()])
        fixture = self

        class TwoFixes:
            role = "implement"

            def play(self, cwd, turn):
                Commit("first fix", path="fix.txt").play(cwd, turn)
                path = "tests/test_thing.py" if touch_test else "other.txt"
                return Commit("second fix", path=path,
                              body="def test_it_works():\n    assert True\n").play(
                                  cwd, turn)

        class PriorApproval:
            role = "review"

            def play(self, cwd, turn):
                approved = fixture.git("rev-parse", "HEAD~2", cwd=cwd).strip()
                return (f"CRITERION 1: met — approval at {approved}; "
                        "tests/test_thing.py::test_it_works\nVERDICT: APPROVE")

        fake, _ = self.loop(Commit("candidate"), APPROVE, Idle(""),
                            Reply("THREAD 1: ADDRESS -- crash"), TwoFixes(),
                            PriorApproval(), Idle(""), provider=self.provider())
        covering = [t for t in fake.turns if t.role == "review"][-1]
        approved, candidate = [sha for _, sha in self.pushed()]
        for text in (approved, candidate, f"{approved}..{candidate}",
                     "first fix", "second fix", "fix.txt", "2 files changed",
                     "Review this range", "do not run the full suite again"):
            self.assertIn(text, covering.goal)
        self.assertNotIn("read the whole candidate", covering.goal)
        self.assertNotIn("Review this range", fake.turns[1].goal)
        self.assertIn("do not run the full suite again", fake.turns[1].goal)
        self.assertEqual(bool([v for k, v in self.api_calls() if k == "merge"]),
                         not touch_test)
        self.assertEqual(self.read("SELECT verdict FROM reviewRounds "
                                   "ORDER BY id")[-1][0],
                         "changes_requested" if touch_test else "pass")

    def test_fix_push_head_catches_up(self):
        self.fix_push_head_propagation(False)

    def test_fix_push_head_stays_stale(self):
        self.fix_push_head_propagation(True)

    def test_ancestor_remote_head_is_not_foreign(self):
        with patch("holophyte.pr_head._remote_head", return_value=self.base):
            self.fix_push_head_propagation(True)

    def fix_push_head_propagation(self, persistent):
        self.configure('[merge]\nmode = "pr"\npr_quiet_sec = 0\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]),
                               self.pr_state(head=self.base)] +
                        ([] if persistent else [self.pr_state(checks="PENDING"),
                                                       self.pr_state()]))
        push = holophyte.pr.push_branch

        def push_with_stale_api(target, branch):
            push(target, branch)
            if len(self.pushed()) == 2:
                previous = self.pushed()[0][1]
                for answer in self.answers.glob("*.json"):
                    answer.write_text(answer.read_text().replace(self.base, previous))

        with patch.object(holophyte.pr, "SLEEP") as sleep, \
                patch.object(holophyte.pr, "push_branch", push_with_stale_api):
            self.loop(Commit("candidate"), APPROVE, Idle(""),
                      Reply("THREAD 1: ADDRESS -- a real crash"),
                      Commit("fix crash"), APPROVE, Idle(""),
                      provider=self.provider())
        self.assertEqual([call.args[0] for call in sleep.call_args_list],
                         [5, 5, 5] if persistent else [5, holophyte.pr.CHECK_POLL_S])
        pushed = self.pushed()[-1][1]
        self.assertEqual(self.read("SELECT outcome FROM runs"),
                         [("merged",)])
        self.assertEqual([v["sha"] for kind, v in self.api_calls()
                          if kind == "merge"], [pushed])
        if persistent:
            events = self.read("SELECT summary FROM runEvents")
            self.assertTrue(any("stale" in row[0] and pushed in row[0]
                                for row in events), events)

    def test_foreign_remote_head_parks_naming_remote(self):
        foreign = self.git("commit-tree", f"{self.base}^{{tree}}",
                           "-m", "foreign root").strip()
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state(head=self.base)])
        with patch("holophyte.pr_head._remote_head", return_value=foreign), \
                patch.object(holophyte.pr, "SLEEP"):
            self.loop(Commit("candidate"), APPROVE, Idle(""),
                      provider=self.provider())
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])
        self.assertIn(f"remote branch head is {foreign[:12]}", self.question())
        self.assertIn("someone else pushed", self.question())
        self.assertFalse([v for kind, v in self.api_calls() if kind == "merge"])

    def test_closed_pr_is_rejected_mid_pass(self):
        self.configure('[merge]\nmode = "pr"\n')
        state = self.pr_state(checks="PENDING")
        node = state["data"]["repository"]["pullRequest"]
        node.update(state="CLOSED", timelineItems={"nodes": [
            {"actor": {"login": "alice"}}]})
        self.fake_route(states=[state])
        with patch.object(
            holophyte.pr, "SLEEP",
            side_effect=AssertionError("closed PR must not wait"),
        ) as sleep:
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=self.provider())
        sleep.assert_not_called()
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("rejected", "rejected")])
        self.assertTrue(self.read("SELECT blockedQuestion FROM tickets")
                        [0][0].startswith("rejected:"))
        self.assertIsNone(self.rc)


    def test_closed_pr_at_pass_cap_is_rejected(self):
        self.configure('[merge]\nmode = "pr"\npr_rounds = 1\n')
        closed = self.pr_state()
        closed["data"]["repository"]["pullRequest"].update(
            state="CLOSED", timelineItems={"nodes": [{"actor": {"login": "alice"}}]})
        self.fake_route(states=[self.pr_state([self.DEFECT]), closed])
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  Reply("THREAD 1: ADDRESS -- a real crash"),
                  Commit("fix: default load()"), provider=self.provider())
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("rejected", "rejected")])
        self.assertIsNone(self.rc)


    def test_a_green_pr_quieter_than_pr_quiet_sec_is_not_merged(self):
        self.configure('[merge]\nmode = "pr"\npr_rounds = 1\ncheck_wait_sec = 45\n')
        fresh = (datetime.now(timezone.utc)
                 - timedelta(seconds=10)).isoformat()
        self.fake_route(states=[self.pr_state(updated_at=fresh)])
        naps = []
        with patch.object(holophyte.pr, "SLEEP", naps.append), \
                patch.object(holophyte.babysitter, "monotonic",
                             side_effect=lambda: sum(naps)):
            out = self.main_output(Commit("the scripted work"), APPROVE, Idle(""),
                                   provider=self.provider())

        self.assertTrue(naps)
        self.assertRegex(out, r"green and quiet for \d+s of the 300s"
                              r" required; waiting")
        calls = self.api_calls()
        self.assertGreater(len(calls), 1)
        self.assertEqual({kind for kind, _ in calls}, {"state"})
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])
        self.assertIn("quiet wait exceeded 45s", self.question())


    def test_a_green_pr_quiet_for_pr_quiet_sec_merges(self):
        self.configure('[merge]\nmode = "pr"\n')
        quiet = (datetime.now(timezone.utc)
                 - timedelta(seconds=301)).isoformat()
        self.fake_route(states=[self.pr_state(updated_at=quiet)])
        naps = []
        with patch.object(holophyte.pr, "SLEEP", naps.append):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=self.provider())

        self.assertEqual(naps, [])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge"])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", self.MERGE_SHA)])


    def test_pr_quiet_sec_zero_merges_a_green_pr_on_the_first_pass(self):
        self.configure('[merge]\nmode = "pr"\npr_quiet_sec = 0\n')
        self.fake_route(states=[self.pr_state(
            updated_at=datetime.now(timezone.utc).isoformat())])
        naps = []
        with patch.object(holophyte.pr, "SLEEP", naps.append):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=self.provider())

        self.assertEqual(naps, [])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge"])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", self.MERGE_SHA)])


    def test_babysit_re_entry_merges_the_approved_sha_without_a_review(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.NIT]), self.pr_state()])
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  Reply("THREAD 1: DECLINE -- a naming preference"),
                  provider=self.provider())
        approved = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(self.read("SELECT candidateSha, approvedSha FROM"
                                   " runs"), [(approved, approved)])
        for path in self.api_dir.iterdir():
            path.unlink()
        holophyte.operator.babysit_ticket(
            self.tgt, "KO-131", holophyte.operator.BABYSIT_DEFAULT_NOTE,
            out=io.StringIO())

        fake, _ = self.loop(provider=self.provider())

        self.assertEqual(fake.roles, [])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge"])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"
                                   " WHERE id = 2"),
                         [("merged", self.MERGE_SHA)])


    def rejected_resume_with_stale_approval(self):
        self.resume_rejected_fix()
        import store
        with closing(store.open(self.tgt.store_path)) as conn:
            # Legacy carried approval metadata must not override a rejection.
            conn.execute("UPDATE runs SET approvedSha = candidateSha WHERE id = 1")
            conn.commit()
        return self.git("rev-parse", BRANCH).strip()

    def test_rejected_resume_reviews_before_merge_despite_carried_approval(self):
        candidate = self.rejected_resume_with_stale_approval()
        fixture = self

        class ReviewBeforeMerge:
            role = APPROVE.role

            def play(self, cwd, turn):
                fixture.assertFalse([v for kind, v in fixture.api_calls()
                                     if kind == "merge"])
                return APPROVE.play(cwd, turn)

        fake, _ = self.loop(ReviewBeforeMerge(), Idle(""), provider=self.provider())
        self.assertEqual(fake.roles, ["review", "implement"])
        self.assertIn("read the whole candidate, the fixes included",
                      fake.turns[0].goal)
        self.assertIn("last review of it asked for changes", fake.turns[0].goal)
        self.assertEqual(fake.turns[0].candidate_sha, candidate)
        self.assertEqual([v["sha"] for kind, v in self.api_calls()
                          if kind == "merge"], [candidate])

    def test_rejected_resume_exhausts_fix_allowance_and_parks_with_findings(self):
        self.rejected_resume_with_stale_approval()
        review = cases.SpentCapReview(self.db, REQUEST_CHANGES)
        fake, _ = self.loop(review, Commit("attempt review fix"), REQUEST_CHANGES,
                            provider=self.provider())
        self.assertEqual(fake.roles, ["review", "implement", "review"])
        self.assertEqual(review.count, 1)
        self.assertFalse([v for kind, v in self.api_calls() if kind == "merge"])
        self.assertIn("scripted change is incomplete", self.question())
        self.assertEqual(self.read("SELECT phase, outcome FROM runs WHERE id = 2"),
                         [("awaiting_merge_approval", None)])

    def test_explicit_approval_of_rejected_candidate_skips_review(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]), self.pr_state()])
        self.loop(Commit("candidate"), APPROVE, Idle(""),
                  Reply("THREAD 1: ADDRESS -- a real crash"), Commit("thread fix"),
                  REQUEST_CHANGES, provider=self.provider())
        candidate = self.git("rev-parse", BRANCH).strip()
        holophyte.operator.approve(self.tgt, "KO-131", "accept this candidate",
                                   out=io.StringIO())
        out = self.main_output(provider=self.provider())
        self.assertEqual(self.last_fake.roles, [])
        self.assertIn("verify ok before merge", out)
        self.assertEqual([v["sha"] for kind, v in self.api_calls()
                          if kind == "merge"], [candidate])


    def test_human_resume_after_launch_loop_waits_without_merging(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route(states=[self.pr_state()])
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  provider=self.provider())
        holophyte.operator.babysit_ticket(
            self.tgt, "KO-131", holophyte.operator.BABYSIT_DEFAULT_NOTE,
            out=io.StringIO())
        import store
        with closing(store.open(self.tgt.store_path)) as conn:
            store.record_intervention(conn, 1, "launch_loop", "resume",
                                      source="supervisor")
        for path in self.api_dir.iterdir():
            path.unlink()

        self.loop(provider=self.provider())

        self.assertFalse([v for kind, v in self.api_calls() if kind == "merge"])
        self.assertEqual(self.read("SELECT phase, outcome FROM runs WHERE id = 2"),
                         [("awaiting_merge_approval", None)])
        self.assertIn("waiting for a human to say merge", self.question())

    def test_threads_past_the_first_page_keep_the_pr_from_reading_quiet(self):
        self.configure('[merge]\nmode = "pr"\n')
        full_page = [self.NIT] * holophyte.pr_status.THREADS_PAGE
        self.fake_route(states=[self.pr_state(resolved=full_page,
                                              next_cursor="c1"),
                                self.pr_state([self.DEFECT])])

        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            Reply("THREAD 1: HUMAN -- not mine to answer"),
                            provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "implement", "adjudicate"])
        calls = self.api_calls()
        self.assertEqual([kind for kind, _ in calls], ["state", "state"])
        self.assertEqual([v["after"] for _, v in calls], [None, "c1"])
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])
        self.assertIn(self.DEFECT[3], self.question())


if __name__ == "__main__":
    unittest.main()
