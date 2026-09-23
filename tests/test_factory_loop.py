"""Factory loop control flow, driven end to end with zero agent calls.

Fake agents script turns; repositories, worktrees, verification and merges are real.
Run: python3 -m unittest discover -s tests -p 'test_factory_loop*' -v
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
# Helper resolution depends on whether unittest uses discovery or module imports.
# Putting it there explicitly makes `discover -s tests` and `-m unittest
# tests.test_factory_loop` resolve the harness the same way.
sys.path.insert(0, str(HERE))
from abort_fixture import AbortTurnCases  # noqa: E402
from failure_kind_fixture import FailureKindCases  # noqa: E402
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    FAIL,
    MALFORMED,
    PASS,
    REQUEST_CHANGES,
    Commit,
    FakeAgent,
    Idle,
    no_agent_processes,
)
from fix_session_fixture import FixSessionCases  # noqa: E402
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    CommitThenTimeout,
    IdleThenTimeout,
    InfraRefuse,
    LoopFixture,
    MergeModeFixture,
    StubProvider,
    a_task,
)
from pause_fixture import PauseFailureCases  # noqa: E402
from review_session_fixture import ReviewSessionCases  # noqa: E402
from run_landing_fixture import landing_path  # noqa: E402

import holophyte.agents  # noqa: E402 - after the sys.path insert above
import holophyte.board  # noqa: E402 - after the sys.path insert above
import holophyte.claim  # noqa: E402 - after the sys.path insert above
import holophyte.config  # noqa: E402 - after the sys.path insert above
import holophyte.config_tables  # noqa: E402 - after the sys.path insert above
import holophyte.gates  # noqa: E402 - after the sys.path insert above
import holophyte.loop  # noqa: E402 - after the sys.path insert above
import holophyte.merge_gate  # noqa: E402 - after the sys.path insert above
import holophyte.operator  # noqa: E402 - after the sys.path insert above
import holophyte.pr  # noqa: E402 - after the sys.path insert above
import holophyte.project  # noqa: E402 - after the sys.path insert above
import holophyte.runs  # noqa: E402 - after the sys.path insert above
import holophyte.supervisor  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets as tickets  # noqa: E402 - after the sys.path insert above


class LoopTests(AbortTurnCases, PauseFailureCases, FailureKindCases,
                ReviewSessionCases, FixSessionCases, LoopFixture):
    def test_pause_after_implement_preserves_work_and_parks_with_note(self):
        from pause_fixture import PauseEdit
        self.loop(PauseEdit(self.db))
        self.assertEqual(self.read("SELECT outcome, resumePhase FROM runs"),
                         [("paused", "verifying")])
        self.assertEqual(self.read("SELECT status, blockedQuestion FROM tickets"),
                         [("blocked_on_operator", "reboot writer")])
        wt = self.worktrees / "ko-131-add-a-thing"
        self.assertEqual((wt / "pause-work.txt").read_text(),
                         "preserve this uncommitted work\n")
        self.assertEqual(self.git("status", "--porcelain", cwd=wt), "")
        events = self.read("SELECT kind, summary FROM runEvents ORDER BY seq")
        request = next(i for i, (kind, _) in enumerate(events)
                       if kind == "intervention")
        release = next(i for i, (_, summary) in enumerate(events)
                       if "outcome paused" in summary)
        self.assertLess(request, release)
        self.assertIn("WIP: preserve work at operator pause", self.subjects(BRANCH))

    def test_illegal_phase_is_infrastructure_failure_and_preserves_work(self):
        original = store.set_phase

        def refuse(conn, run_id, phase, *args, **kwargs):
            if phase == "verifying":
                raise store.IllegalTransition(run_id, "merge_gate", "working")
            return original(conn, run_id, phase, *args, **kwargs)

        with patch.object(store, "set_phase", side_effect=refuse):
            self.loop(Commit("candidate"))
        ((outcome, kind, reason),) = self.read(
            "SELECT outcome, outcomeClass, outcomeReason FROM runs")
        self.assertEqual((outcome, kind), ("failed", "infra"))
        self.assertIn("merge_gate -> working", reason)
        self.assertIn("run 1", reason)
        self.assertTrue((self.worktrees / "ko-131-add-a-thing").is_dir())
        self.assertIn("candidate", self.subjects(BRANCH))

    def test_implementer_sessions_survive_implement_and_fix_turns(self):
        self.configure("[agents]\nimplementer_session = 'session id: ([a-z-]+)'\n")

        class SessionCommit(Commit):
            def play(step, cwd, turn):
                super().play(cwd, turn)
                return f"session id: {step.message}"

        seen = []

        class ObserveCommit(SessionCommit):
            def play(step, cwd, turn):
                seen.extend(self.read("SELECT providerSessionId FROM runs"))
                self.assertEqual(self.read(
                    "SELECT count(*) FROM runEvents WHERE kind = 'agent_session'"),
                    [(1,)])
                return super().play(cwd, turn)

        self.loop(SessionCommit("first-session"), REQUEST_CHANGES,
                  ObserveCommit("fix-session"), APPROVE)
        self.assertEqual(seen, [("first-session",)])
        self.assertEqual(self.read("SELECT providerSessionId, outcome FROM runs"),
                         [("fix-session", "merged")])
        events = self.read(
            "SELECT payload FROM runEvents WHERE kind = 'agent_session' ORDER BY seq")
        self.assertEqual([json.loads(row[0]) for row in events], [
            {"session_id": session, "role": "implement", "route": "primary"}
            for session in ("first-session", "fix-session")])

    def test_session_recording_is_optional_and_requires_a_match(self):
        for number, (config, output) in enumerate((
            ("", "session id: ignored"),
            ("[agents]\nimplementer_session = 'session id: ([a-z-]+)'\n",
             "no banner"),
        ), 1):
            with self.subTest(config=config):
                self.configure(config)
                self.loop(Commit(output), APPROVE,
                          provider=StubProvider(a_task(number)))
                self.assertTrue(all(value is None for (value,) in
                                    self.read("SELECT providerSessionId FROM runs")))
                self.assertEqual(self.read(
                    "SELECT payload FROM runEvents WHERE kind = 'agent_session'"), [])
                self.assertEqual(self.read("SELECT outcome FROM runs"),
                                 [("merged",)] * number)

    def test_timed_out_fallback_session_is_recorded(self):
        self.configure("[agents]\nimplementer_session = 'session id: ([a-z-]+)'\n")

        class CappedSession(Commit):
            def play(step, cwd, turn):
                super().play(cwd, turn)
                holophyte.agents.routes(self.tgt).commands["implement"] = "fallback-cli"
                raise subprocess.TimeoutExpired(
                    "fallback-cli", 1, output=b"session id: capped-session")

        self.loop(CappedSession("partial work"), APPROVE)
        self.assertEqual(self.read("SELECT providerSessionId FROM runs"),
                         [("capped-session",)])
        events = self.read(
            "SELECT payload FROM runEvents WHERE kind = 'agent_session'")
        self.assertEqual([json.loads(row[0]) for row in events], [
            {"session_id": "capped-session", "role": "implement", "route": "fallback"}])

    def test_writer_and_container_turns_do_not_record_sessions(self):
        self.configure("[agents]\nimplementer_session = 'session id: ([a-z-]+)'\n")
        self.loop(Commit("seed run"), APPROVE)
        conn = store.open(self.db)
        self.addCleanup(conn.close)
        run_id = self.read("SELECT id FROM runs")[0][0]
        # The writer may use the implementer CLI but remains out of scope.
        with (patch.object(holophyte.loop, "heartbeat_while",
                           return_value=contextlib.nullcontext()),
              patch.object(holophyte.loop, "agent",
                           return_value="session id: excluded")):
            holophyte.loop._timed(self.tgt, conn, run_id, 1, self.target, 1,
                                  "write", role="write")
            self.configure("[agents]\nimplementer_isolation = 'container'\n"
                           "implementer_session = 'session id: ([a-z-]+)'\n")
            holophyte.loop._timed(self.tgt, conn, run_id, 1, self.target, 1,
                                  "implement")

        self.assertEqual(self.read("SELECT providerSessionId FROM runs"), [(None,)])
        self.assertEqual(self.read(
            "SELECT payload FROM runEvents WHERE kind = 'agent_session'"), [])

    def test_startup_first_line_names_running_build(self):
        # The factory build comes from its source checkout, even when its
        # target is another repository. Keep the normal self-hosting decision.
        sha = self.git("rev-parse", "--short", "HEAD", cwd=ROOT).strip()
        output = self.main_output(Commit("banner witness"), APPROVE)
        self.assertEqual(output.splitlines()[0], f"[holo2] factory at {sha}")

    # --- the clean run ---------------------------------------------------

    def test_a_script_ending_in_approve_merges_without_spawning_an_agent(self):
        """Approval merges the candidate without launching a real agent."""
        fake, guard = self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(guard.spawned, [])
        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertIn("the scripted work", self.subjects())
        self.assertNotIn(BRANCH, self.branches())  # merged, so deleted
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    # --- the ledger: store row first, board comment second ---------------

    def _ledger_reading_provider(self):
        """A provider stub that, at every `comment()`, reads `ledger` over
        its own connection before posting, so the test can see what the
        store held at the moment each comment went out."""
        db = self.db

        class ReadingProvider(StubProvider):
            def comment(self, task_id, body):
                raw = sqlite3.connect(db)
                try:
                    self.seen.append([
                        (kind, text) for (kind, text) in raw.execute(
                            "SELECT kind, text FROM ledger ORDER BY id")])
                finally:
                    raw.close()
                super().comment(task_id, body)

        provider = ReadingProvider(a_task())
        provider.seen = []
        return provider

    def test_a_clean_approval_records_its_round_before_the_merge(self):
        """The approving round reaches the ledger before the merge."""
        provider = self._ledger_reading_provider()
        self.loop(Commit("first cut"), APPROVE, provider=provider)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        (run_id,) = self.read("SELECT id FROM runs")[0]
        # The review-cap note opens the narrative (KO-299), then the
        # approving round, then the merge.
        self.assertEqual(
            self.read("SELECT runId, kind FROM ledger ORDER BY at, id"),
            [(run_id, "note"), (run_id, "round"), (run_id, "merge")])
        self.assertEqual([len(seen) for seen in provider.seen], [1, 2, 3])
        self.assertEqual([seen[-1][0] for seen in provider.seen],
                         ["note", "round", "merge"])
        for (_task, body), seen in zip(provider.comments, provider.seen):
            self.assertIn(seen[-1][1], body)

    def test_the_ledger_row_is_in_the_store_before_its_board_comment(self):
        """Store ledger entries precede the corresponding board comments."""
        provider = self._ledger_reading_provider()
        self.loop(Commit("first cut"), REQUEST_CHANGES, Commit("fix round 1"),
                  APPROVE, provider=provider)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        (run_id,) = self.read("SELECT id FROM runs")[0]
        entries = self.read(
            "SELECT runId, kind, source FROM ledger ORDER BY at, id")
        self.assertEqual(entries, [(run_id, "note", "loop"),
                                   (run_id, "round", "loop"),
                                   (run_id, "round", "loop"),
                                   (run_id, "merge", "loop")])
        # Four comments, and at each one the table already held the entry
        # the comment carries: one row at the review-cap note's comment, two
        # at the findings round's, three at the approving round's, four at
        # the merge's.
        self.assertEqual(len(provider.comments), 4)
        self.assertEqual([len(seen) for seen in provider.seen], [1, 2, 3, 4])
        for (_task, body), seen in zip(provider.comments, provider.seen):
            kind, text = seen[-1]
            self.assertIn(text, body)
        self.assertEqual([seen[-1][0] for seen in provider.seen],
                         ["note", "round", "round", "merge"])

    # --- both rounds spent, then a terminal PASS -------------------------

    def test_two_findings_rounds_then_adjudication_pass_merges_the_fixes(self):
        """Two review fixes followed by terminal PASS merge the candidate."""
        fake, _ = self.loop(Commit("first cut"), REQUEST_CHANGES,
                            Commit("fix round 1"), REQUEST_CHANGES,
                            Commit("fix round 2"), PASS)

        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "review", "implement", "adjudicate"])
        self.assertEqual(self.transitions(),
                         ["claimed -> working",
                          "working -> verifying",
                          "verifying -> reviewing",
                          "reviewing -> addressing",
                          "addressing -> verifying",
                          "verifying -> reviewing",
                          "reviewing -> addressing",
                          "addressing -> verifying",
                          "verifying -> reviewing",
                          "reviewing -> merge_gate",
                          "merge_gate -> merging",
                          "merging -> done"])
        subjects = self.subjects()
        self.assertIn("fix round 1", subjects)
        self.assertIn("fix round 2", subjects)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_review_cap_from_config_admits_a_third_round(self):
        """Configured review limits permit a third round for a large candidate."""
        self.configure("[loop]\nreview_rounds = 2\n"
                       "review_rounds_per_lines = 1\nreview_rounds_max = 3\n")
        fake, _ = self.loop(Commit("first cut"), REQUEST_CHANGES,
                            Commit("fix round 1"), REQUEST_CHANGES,
                            Commit("fix round 2"), APPROVE)

        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "review", "implement", "review"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual(
            self.read("SELECT round, verdict FROM reviewRounds ORDER BY round"),
            [(1, "changes_requested"), (2, "changes_requested"),
             (3, "pass")])
        self.assertIn("fix round 2", self.subjects())
        self.assertEqual(
            self.read("SELECT text FROM ledger WHERE kind = 'note'"),
            [("Review cap 3 for 1 changed lines",)])
        self.assertEqual(self.read("SELECT reviewRoundCap FROM runs"), [(3,)])

        # The same script under the default config: two rounds, then the
        # adjudicator's turn, which an APPROVE reply is no verdict for.
        self.setUp()
        fake, _ = self.loop(Commit("first cut"), REQUEST_CHANGES,
                            Commit("fix round 1"), REQUEST_CHANGES,
                            Commit("fix round 2"), APPROVE)

        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "review", "implement", "adjudicate"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(
            self.read("SELECT text FROM ledger WHERE kind = 'note'"),
            [("Review cap 2 for 1 changed lines",)])
        self.assertEqual(self.read("SELECT reviewRoundCap FROM runs"), [(2,)])
        self.assertIn("after 2 review rounds", self.read(
            "SELECT text FROM ledger WHERE kind = 'adjudication'")[0][0])

    # --- adjudication refuses --------------------------------------------

    def test_adjudication_fail_preserves_the_branch_and_stops_the_loop(self):
        """Terminal FAIL preserves work and stops the loop."""
        provider = StubProvider(a_task(1), a_task(2))
        self.loop(Commit("first cut"), REQUEST_CHANGES,
                  Commit("fix round 1"), REQUEST_CHANGES,
                  Commit("fix round 2"), FAIL, provider=provider)

        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertIn(BRANCH, self.branches())
        self.assertIn("fix round 2", self.subjects(BRANCH))
        self.assertTrue((self.worktrees / "ko-131-add-a-thing").exists())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(len(provider.queue), 1)  # the loop stopped

    def test_an_adjudication_reply_with_no_verdict_is_read_as_fail(self):
        """An absent verdict fails closed."""
        self.loop(Commit("first cut"), REQUEST_CHANGES,
                  Commit("fix round 1"), REQUEST_CHANGES,
                  Commit("fix round 2"), MALFORMED)

        self.assertEqual(self.read("SELECT failureKind FROM runs"),
                         [("review_route",)])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertIn(BRANCH, self.branches())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(
            self.read("SELECT round, verdict FROM reviewRounds ORDER BY round"),
            [(1, "changes_requested"), (2, "changes_requested"), (3, "error")])

    # --- merge-time drift ------------------------------------------------

    def test_a_ticket_edited_during_the_run_is_not_merged(self):
        """Ticket drift prevents merging a candidate built to an older contract."""
        provider = StubProvider(a_task())
        provider.live["iss-131"] = dict(
            a_task(), title="add a thing, and a second thing",
            criteria=["Given the thing, when it runs, then it works twice"])

        self.loop(Commit("the scripted work"), APPROVE, provider=provider)

        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertIn(BRANCH, self.branches())
        self.assertIn("the scripted work", self.subjects(BRANCH))
        self.assertTrue((self.worktrees / "ko-131-add-a-thing").exists())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        # The approving round's comment precedes the refusal's.
        (_, body) = provider.comments[-1]
        self.assertIn("MERGE REFUSED", body)
        for field in ("title", "acceptanceCriteria"):
            self.assertIn(field, body)

    def test_a_ticket_that_cannot_be_re_read_still_merges(self):
        """Missing board evidence alone is not ticket drift."""
        provider = StubProvider(a_task())

        def unreachable(issue_id):
            raise RuntimeError("linear is down")

        provider.fetch_task = unreachable

        self.loop(Commit("the scripted work"), APPROVE, provider=provider)

        self.assertIn("the scripted work", self.subjects())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        warnings = self.read("SELECT summary FROM runEvents"
                             " WHERE kind = 'warning'")
        self.assertEqual(len(warnings), 1)
        self.assertIn("linear is down", warnings[0][0])

    # --- failure-pattern escalation --------------------------------------

    def fail_once(self, tag="first"):
        """Drive one whole run of the ticket that ends in a terminal FAIL.

        `tag` keeps a rerun's scripted commits distinct: a run after an
        unblock reuses the preserved worktree, and re-writing identical
        files would leave the scripted `git commit` nothing to commit.
        """
        self.loop(Commit(f"{tag} cut"), REQUEST_CHANGES,
                  Commit(f"{tag} fix round 1"), REQUEST_CHANGES,
                  Commit(f"{tag} fix round 2"), FAIL)

    def offer_again(self):
        """Offer the same ticket back, as a stale board does. No agent turns.

        The loop never reaches an agent on this pass, so the empty script is
        not laziness: a turn asked for here would be the loop re-implementing
        a ticket it had already failed on, and `FakeAgent` fails the test
        rather than answering one. The store still says `in_flight`, so the
        claim path skips the offer before any run is opened.
        """
        provider = StubProvider(a_task())
        self.loop(provider=provider)
        return provider

    def drag_back(self):
        """The ticket walked back to `ready` with nothing recorded — the
        store's view of a board drag, which forgives nothing — so the next
        offer opens a run that does the work again."""
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        tickets.walk_ticket(conn, 1, "ready")

    def fail_again(self, tag="retry"):
        """A second run that fails on the work: the second `work` strike."""
        self.drag_back()
        self.fail_once(tag)

    def status(self):
        (status,), = self.read("SELECT status FROM tickets")
        return status

    def attempts(self):
        return self.read("SELECT attempt FROM runs ORDER BY id")

    def test_one_failed_run_leaves_the_ticket_claimable(self):
        """One work failure leaves the ticket eligible after a board drag."""
        self.fail_once()

        self.assertEqual(self.status(), "in_flight")

        self.fail_again()

        self.assertEqual(self.attempts(), [(1,), (2,)])

    def test_the_second_failure_blocks_the_ticket_and_reports_both_runs(self):
        """Two work failures park the ticket and report both attempts."""
        self.fail_once()

        provider = self.offer_again()  # skipped: the store says in_flight
        self.assertEqual(self.status(), "in_flight")
        self.assertEqual(provider.comments, [])

        provider = self.offer_again()  # and again: still nothing to say
        self.assertEqual(self.status(), "in_flight")
        self.assertEqual(provider.comments, [])

        self.fail_again()

        self.assertEqual(self.status(), "blocked_on_operator")
        self.assertIn(("iss-131", "Todo"), self.last_provider.states)
        (issue_id, body), = [(issue, body) for issue, body
                             in self.last_provider.comments
                             if body.startswith("**Blocked after")]
        self.assertEqual(issue_id, "iss-131")
        counted = self.read("SELECT attempt, outcomeReason FROM runs"
                            " WHERE outcome = 'failed'"
                            " AND outcomeClass = 'work' ORDER BY attempt")
        self.assertEqual([a for a, _ in counted], [1, 2])
        self.assertIn("Blocked after 2 failed runs", body)
        for attempt, reason in counted:
            self.assertIn(f"attempt {attempt}: {reason}", body)
        # The two skipped offers left no run row behind: they are not on the
        # record as failed runs, so neither `--report` nor the comment can
        # mistake them for attempts.
        self.assertEqual(self.attempts(), [(1,), (2,)])

    def test_an_offered_back_ticket_the_store_holds_in_flight_is_skipped(self):
        """Stale board offers cannot reclaim a ticket still in flight."""
        self.fail_once()

        with patch("builtins.print") as printed:
            self.offer_again()

        self.assertEqual(self.attempts(), [(1,)])
        self.assertEqual(self.status(), "in_flight")
        notes = [c.args[0] for c in printed.call_args_list
                 if c.args and "skipping" in str(c.args[0])]
        self.assertEqual(len(notes), 1)
        self.assertIn(a_task()["id"], notes[0])
        self.assertIn("in_flight", notes[0])

    def test_a_low_complexity_budget_claims_nothing(self):
        """KO-434 review: the serial claim's `claim_next()` spends the same
        ready listing the queue mirror skipped, so under a tenth of the
        key's hourly limit the pass ends on the reset line -- the provider
        never asked -- rather than claiming to be refused. `loop()` stands
        the provider in for `sys.modules["linear_provider"]`, so the budget
        the guards read is the provider's own attribute."""
        import linear_provider
        budget = linear_provider.LinearBudget()
        budget.remember({
            "x-ratelimit-complexity-limit": "3000000",
            "x-ratelimit-complexity-remaining": "200000",
            "x-ratelimit-complexity-reset": "9999999999999"})
        provider = StubProvider(a_task())
        provider.LINEAR_BUDGET = budget
        asked = []
        claim_next = provider.claim_next
        provider.claim_next = \
            lambda **kw: asked.append("claim") or claim_next(**kw)
        ready_issues = provider.ready_issues
        provider.ready_issues = \
            lambda: asked.append("mirror") or ready_issues()

        out = self.main_output(provider=provider)

        self.assertEqual(asked, [])
        self.assertEqual(self.rc, 1)
        self.assertIn("board not asked: budget resets at", out)
        self.assertEqual(out.count("board not asked"), 1)
        self.assertNotIn("Linear has no ready tickets", out)

    def test_a_mirror_answer_making_the_budget_low_claims_nothing(self):
        """KO-434 review: the mirror's own answer can be the spend that
        pushes the complexity budget under its tenth, and the serial
        claim's `claim_next()` lists the same queue -- so the pass ends
        on the reset line with the claim never asked rather than asking
        to be refused. `ready_issues()` remembers the low headers the way
        `_gql()` does on the real board; `loop()` stands the provider in
        for `sys.modules["linear_provider"]`, so the budget the guards
        read is the provider's own attribute."""
        import linear_provider
        provider = StubProvider(a_task())
        provider.LINEAR_BUDGET = linear_provider.LinearBudget()
        asked = []
        ready_issues = provider.ready_issues

        def mirror():
            asked.append("mirror")
            listing = ready_issues()
            provider.LINEAR_BUDGET.remember({
                "x-ratelimit-complexity-limit": "3000000",
                "x-ratelimit-complexity-remaining": "200000",
                "x-ratelimit-complexity-reset": "9999999999999"})
            return listing

        provider.ready_issues = mirror
        claim_next = provider.claim_next
        provider.claim_next = \
            lambda **kw: asked.append("claim") or claim_next(**kw)

        out = self.main_output(provider=provider)

        self.assertEqual(asked, ["mirror"])
        self.assertEqual(self.rc, 1)
        self.assertIn("board not asked: budget resets at", out)
        self.assertEqual(out.count("board not asked"), 1)
        self.assertNotIn("Linear has no ready tickets", out)

    def test_a_mirror_429_making_the_budget_low_claims_nothing(self):
        """KO-434 review: a refusal is the same transition. The 429's
        headers are what make the budget low, the mirror's catch-all
        turns the raise into a skipped listing, and the claim's
        `claim_next()` must still not spend the listing to be refused
        a second time."""
        import linear_provider
        provider = StubProvider(a_task())
        provider.LINEAR_BUDGET = linear_provider.LinearBudget()
        asked = []

        def mirror():
            asked.append("mirror")
            provider.LINEAR_BUDGET.remember({
                "x-ratelimit-complexity-limit": "3000000",
                "x-ratelimit-complexity-remaining": "0",
                "x-ratelimit-complexity-reset": "9999999999999"})
            raise linear_provider.LinearBudgetExhausted(
                "Linear refused the query (429): the API key's complexity"
                " budget is spent", reset_at=9999999999999)

        provider.ready_issues = mirror
        claim_next = provider.claim_next
        provider.claim_next = \
            lambda **kw: asked.append("claim") or claim_next(**kw)

        out = self.main_output(provider=provider)

        self.assertEqual(asked, ["mirror"])
        self.assertEqual(self.rc, 1)
        self.assertIn("queue mirror skipped", out)
        self.assertIn("board not asked: budget resets at", out)
        self.assertEqual(out.count("board not asked"), 1)
        self.assertNotIn("Linear has no ready tickets", out)

    def test_an_infra_failure_raised_by_the_run_is_closed_out_as_infra(self):
        self.loop(InfraRefuse())

        self.assertEqual(self.rc, 1)
        self.assertEqual(
            self.read("SELECT outcome, outcomeClass, outcomeReason FROM runs"),
            [("failed", "infra", "the reviewer container did not start")])
        self.assertEqual(self.read("SELECT failureKind FROM runs"), [("infra",)])
        self.assertEqual(self.status(), "in_flight")

    def test_infra_failures_alone_never_block_the_ticket(self):
        """Infrastructure failures never spend the ticket's work strikes."""
        self.loop(InfraRefuse())
        self.drag_back()
        provider = StubProvider(a_task())
        self.loop(InfraRefuse(), provider=provider)

        self.assertEqual(
            self.read("SELECT outcomeClass FROM runs WHERE outcome = 'failed'"),
            [("infra",), ("infra",)])
        self.assertEqual(self.status(), "in_flight")
        self.assertEqual(provider.comments, [])
        # And a third, real attempt is still let through.
        self.drag_back()
        self.loop(Commit("third time"), APPROVE)
        self.assertEqual(self.status(), "merged")

    def test_a_blocked_ticket_is_not_claimed_again(self):
        """A blocked ticket cannot be reclaimed through a stale board offer."""
        self.fail_once()
        self.fail_again()
        blocked_at = self.attempts()

        provider = self.offer_again()

        self.assertEqual(self.attempts(), blocked_at)  # nothing was claimed
        self.assertEqual(provider.states, [])
        self.assertEqual(provider.comments, [])
        self.assertEqual(self.status(), "blocked_on_operator")

    def intervene(self, source):
        """A recorded intervention on the ticket's newest run, then the §3
        walk back to claimable — the in-band unblock, as an operator does it
        through the store API."""
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        ((last_run,),) = self.read("SELECT MAX(id) FROM runs")
        store.record_intervention(
            conn, last_run, "close_out",
            "reviewed the failures and released the ticket", source=source)
        tickets.walk_ticket(conn, 1, "ready")

    def test_a_recorded_human_intervention_grants_a_fresh_count(self):
        """A recorded human intervention resets the work-failure count."""
        self.fail_once()
        self.fail_again()  # second failure parks it
        self.intervene("human")

        self.fail_once("third")  # first failure since the human acted

        self.assertEqual(self.status(), "in_flight")  # not re-parked

        self.fail_again("fourth")  # second failure since: the pattern is back

        self.assertEqual(self.status(), "blocked_on_operator")

    def test_a_hand_closed_run_is_dispositioned_not_a_carried_strike(self):
        """A recorded manual close-out excludes that run from work strikes."""
        self.fail_once()
        self.fail_again()  # second failure parks it
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        ((last_run,),) = self.read("SELECT MAX(id) FROM runs")
        t1 = int(time.time() * 1000)
        store.record_intervention(conn, last_run, "close_out",
                                  "operator dispositioned the failure",
                                  now=t1)
        # The unlucky ordering, pinned explicitly: the release stamped one
        # millisecond after the intervention's record.
        conn.execute("UPDATE runs SET endedAt = ? WHERE id = ?",
                     (t1 + 1, last_run))
        conn.commit()

        self.assertEqual(holophyte.board.failure_history(conn, 1), [])

    def test_a_supervisor_intervention_grants_no_amnesty(self):
        """Supervisor intervention does not reset the work-failure count."""
        self.fail_once()
        self.fail_again()
        self.intervene("supervisor")

        self.fail_once("third")

        self.assertEqual(self.status(), "blocked_on_operator")

    def test_a_blocked_ticket_is_skipped_rather_than_stopped_on(self):
        """A blocked ticket does not prevent claiming the next ready ticket."""
        self.fail_once()
        self.fail_again()
        blocked, other = a_task(), dict(a_task(2), title="add another thing")

        out = self.main_output(Commit("the other work"), APPROVE,
                               provider=StubProvider(blocked, other))

        self.assertIn("[holo2] KO-131 struck out after 2 failures;"
                      " a human owns it now\n", out)
        self.assertIn("the other work", self.subjects())
        self.assertEqual(
            self.read("SELECT linearIdentifier, status FROM tickets"
                      " ORDER BY id"),
            [("KO-131", "blocked_on_operator"), ("KO-132", "merged")])
        # And the skip is per pass, not per ticket offered: the blocked one
        # opened no run, so every run row belongs to KO-131's two failures and
        # KO-132's merge.
        self.assertEqual(
            self.read("SELECT t.linearIdentifier, r.outcome FROM runs r"
                      " JOIN tickets t ON t.id = r.ticketId ORDER BY r.id"),
            [("KO-131", "failed"), ("KO-131", "failed"), ("KO-132", "merged")])

    def test_branch_is_recorded_at_worktree_cut(self):
        """Record the branch before the first working-phase event."""
        seen = []
        real = holophyte.claim.set_phase

        def watching(conn, run_id, phase, note=None):
            (branch,) = conn.execute(
                "SELECT branch FROM runs WHERE id = ?", (run_id,)).fetchone()
            seen.append((phase, branch))
            return real(conn, run_id, phase, note)

        with patch.object(holophyte.claim, "set_phase", watching):
            self.loop(Commit("the scripted work"), APPROVE)

        first_working = next(entry for entry in seen if entry[0] == "working")
        self.assertEqual(first_working, ("working", BRANCH))
        self.assertEqual(self.read("SELECT branch FROM runs"), [(BRANCH,)])


class RefreshMainLoopTests(LoopFixture):
    """Every cut starts from everything already on `main` anywhere (KO-378):
    the checkout fetches `origin` and fast-forwards its `main` when behind,
    keeps it when ahead, and refuses to cut when the two diverged. A bare
    repository stands in for origin; a second clone is the other seat that
    commits to it after the checkout last saw it.
    """

    def setUp(self):
        super().setUp()
        self.bare = self.target.parent / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", str(self.bare))
        self.git("remote", "add", "origin", str(self.bare))
        self.git("push", "-q", "origin", "main")
        self.seat = self.target.parent / "seat"
        self.git("clone", "-q", str(self.bare), str(self.seat))
        self.git("config", "user.email", "seat@example.invalid", cwd=self.seat)
        self.git("config", "user.name", "Other Seat", cwd=self.seat)

    def push_from_seat(self, name):
        """A commit made to origin's `main` from another seat."""
        (self.seat / name).write_text(f"{name}\n")
        self.git("add", "-A", cwd=self.seat)
        self.git("commit", "-q", "-m", name, cwd=self.seat)
        self.git("push", "-q", "origin", "main", cwd=self.seat)
        return self.git("rev-parse", "main", cwd=self.seat).strip()

    def commit_locally(self, name):
        """An unpushed commit on the checkout's `main` (a local-mode merge)."""
        (self.target / name).write_text(f"{name}\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", name)
        return self.git("rev-parse", "main").strip()

    def first_branch_parent(self):
        return self.git("rev-parse", f"{BRANCH}~1").strip()

    def test_a_checkout_behind_origin_is_fast_forwarded_before_the_cut(self):
        remote = self.push_from_seat("from-the-other-seat")
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)

        # The turn commits and times out, so the branch is preserved with
        # its commit and `main` is left as the cut found it.
        self.loop(CommitThenTimeout("the scripted work"))

        self.assertEqual(self.first_branch_parent(), remote)
        self.assertEqual(self.git("rev-parse", "main").strip(), remote)

    def test_a_checkout_ahead_of_origin_keeps_its_main(self):
        ahead = self.commit_locally("unpushed-local-merge")

        self.loop(CommitThenTimeout("the scripted work"))

        self.assertEqual(self.git("rev-parse", "main").strip(), ahead)
        self.assertEqual(self.first_branch_parent(), ahead)

    def test_a_diverged_checkout_refuses_the_cut_naming_both_shas(self):
        remote = self.push_from_seat("from-the-other-seat")
        local = self.commit_locally("unpushed-local-merge")

        # An empty script: any agent turn would raise, and none must run.
        self.loop()

        self.assertEqual(self.rc, 1)
        ((outcome, klass, reason),) = self.read(
            "SELECT outcome, outcomeClass, outcomeReason FROM runs")
        self.assertEqual((outcome, klass), ("failed", "infra"))
        self.assertIn("diverged", reason)
        self.assertIn(local, reason)
        self.assertIn(remote, reason)
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        self.assertNotIn(BRANCH, self.branches())
        self.assertEqual(self.git("rev-parse", "main").strip(), local)


class RunCapTests(LoopFixture):
    """`[supervisor] run_cap`: the run's hard ceiling, in boxes (KO-416).

    A run that keeps earning turns by failing review is what the per-turn
    budget cannot bound; the cap refuses the turn that would carry the run
    past it -- before the turn starts, so nothing is killed mid-edit and
    the candidate is preserved for the requeue.
    """

    def aged(self, minutes):
        """Backdate the live run's `startedAt` when the first review starts.

        Through the loop's own `set_phase` seam and `store.transaction()`,
        so the row ages the way a real run does -- the clock the check reads
        is the store's, not a simulated one. Once: later rounds' `reviewing`
        transitions pass through untouched.
        """
        real = holophyte.loop.set_phase
        done = []

        def watching(conn, run_id, phase, note=None):
            if phase == "reviewing" and not done:
                done.append(True)
                with store.transaction(conn):
                    conn.execute(
                        "UPDATE runs SET workingMs = workingMs + ?"
                        " WHERE id = ?", (int(minutes * 60 * 1000), run_id))
            return real(conn, run_id, phase, note)

        return patch.object(holophyte.loop, "set_phase", watching)

    def test_a_fix_turn_the_cap_has_no_room_for_is_refused(self):
        """Refuse a fix turn that cannot fit within the run's remaining cap."""
        self.configure("[supervisor]\nrun_cap = 3\n")
        task = dict(a_task(), budget_min=30)

        with self.aged(70):
            fake, _ = self.loop(Commit("work"), REQUEST_CHANGES,
                                provider=StubProvider(task))

        # One implement turn and one review; the fix turn never ran.
        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        candidate = self.git("rev-parse", BRANCH).strip()
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("out of time", reason)
        self.assertIn("min of agent work against a 30 min box", reason)
        self.assertIn("cap 3x", reason)
        self.assertIn(candidate[:12], reason)
        self.assertIn("open findings", reason)
        ((event,),) = self.read(
            "SELECT summary FROM runEvents WHERE kind = 'run_cap'")
        self.assertIn(candidate[:12], event)
        # The candidate stands for the requeue: branch and worktree kept.
        self.assertIn(BRANCH, self.branches())
        self.assertTrue((self.worktrees / "ko-131-add-a-thing").is_dir())

    def test_a_fix_turn_the_cap_still_has_room_for_runs(self):
        """A larger configured cap admits the same fix turn."""
        self.configure("[supervisor]\nrun_cap = 4\n")
        task = dict(a_task(), budget_min=30)

        with self.aged(70):
            fake, _ = self.loop(Commit("work"), REQUEST_CHANGES,
                                Commit("fix"), APPROVE,
                                provider=StubProvider(task))

        self.assertEqual(fake.roles,
                         ["implement", "review", "implement", "review"])
        self.assertEqual(self.read("SELECT outcome FROM runs"),
                         [("merged",)])
        self.assertEqual(
            self.read("SELECT id FROM runEvents WHERE kind = 'run_cap'"), [])


if __name__ == "__main__":
    unittest.main()


class GateConflictImplementerTests(LoopFixture):
    """A merge-gate conflict goes to the implementer before it fails the
    run (KO-404).

    The gate's merge of `main` into the branch conflicts: before the park
    `GateConflictRequeueTests` covers, the gate runs the same
    conflict-resolution turn the claim path runs on a leftover mid-merge
    worktree (KO-355), in the same worktree. A turn that leaves the merge
    committed sends the gate on to its verify on the merged sha; one that
    does not leaves the run failing exactly as it did before.
    """

    def conflicted(self):
        """KO-131's branch and a moved main both rewrote README.md; the
        worktree is registered and the run claimed. Returns
        `(conn, run_id, branch, wt, sha)`; `conn` closes at cleanup."""
        branch = "task/ko-131"
        self.git("checkout", "-q", "-b", branch)
        (self.target / "README.md").write_text("branch\n")
        self.git("commit", "-qam", "branch side")
        self.git("checkout", "-q", "main")
        (self.target / "README.md").write_text("main\n")
        self.git("commit", "-qam", "main side")
        wt = self.worktrees / "ko-131"
        self.git("worktree", "add", "-q", str(wt), branch)
        sha = self.git("rev-parse", branch, cwd=wt).strip()
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        project = tickets.ensure_project(conn, StubProvider.TEAM,
                                       str(self.target))
        ticket = holophyte.board.mirror_task(conn, project, a_task())
        run_id = store.claim(conn, project, ticket)
        tickets.transition(conn, ticket, "in_flight")
        store.set_branch(conn, run_id, branch)
        return conn, run_id, branch, wt, sha

    def test_a_resolved_gate_conflict_goes_on_to_verify_on_the_merged_sha(
            self):
        provider = StubProvider(a_task())
        conn, run_id, branch, wt, sha = self.conflicted()
        # The verify writes the sha it ran on, outside the worktree: the
        # gate's own word for which commit the check saw.
        seen = self.target.parent / "verify-ran-on.txt"
        fake = FakeAgent(Commit("merge main into the branch",
                                path="README.md", body="merged\n"))
        with patch.object(holophyte.loop, "agent", fake):
            ok, merged = holophyte.merge_gate._merge_gate(
                self.tgt, conn, run_id, provider, "KO-131", "iss-131",
                branch, wt, 60, sha,
                f"git rev-parse HEAD > '{seen}'", [], "add a thing", 5)

        # The verify passed on the sha the implementer's merge commit left.
        self.assertTrue(ok)
        self.assertNotEqual(merged, sha)
        self.assertEqual(seen.read_text().strip(), merged)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt).strip(),
                         merged)
        parents = self.git("rev-list", "--parents", "-n", "1", merged,
                           cwd=wt).split()[1:]
        self.assertEqual(len(parents), 2)
        self.assertIn(self.git("rev-parse", "main").strip(), parents)
        self.assertEqual(fake.roles, ["implement"])
        self.assertIn("README.md", fake.turns[0].goal)
        self.assertEqual(
            self.read("SELECT summary FROM runEvents WHERE summary LIKE"
                      " '%resolved by the implementer%'"),
            [("gate conflict on README.md resolved by the implementer"
              f" at {merged[:12]}",)])
        self.assertTrue(
            any("gate conflict on README.md resolved by the implementer"
                in text
                for (text,) in self.read("SELECT text FROM ledger")))

    def test_an_unresolved_gate_conflict_fails_and_parks_as_before(self):
        provider = StubProvider(a_task())
        conn, run_id, branch, wt, sha = self.conflicted()

        class StageThenReEdit:
            """A resolution turn that stages its fix, then edits the file
            again without committing -- the staged half is one `git merge
            --abort` will not drop, so the unwind has to go past it."""

            role = "implement"

            @staticmethod
            def play(cwd, turn):
                (cwd / "README.md").write_text("resolved\n")
                subprocess.run(["git", "add", "README.md"], cwd=cwd,
                               check=True, capture_output=True)
                (cwd / "README.md").write_text("edited again\n")
                return "staged the resolution, then kept editing it"

        with patch.object(holophyte.loop, "agent",
                          FakeAgent(StageThenReEdit())):
            with self.assertRaises(holophyte.gates.RunFailure) as failed:
                holophyte.merge_gate._sync_main_into_branch(
                    self.tgt, conn, run_id, provider, "KO-131", branch,
                    wt, sha, 60, "add a thing", 5)

        self.assertEqual(
            str(failed.exception),
            f"merging main into {branch} conflicted on: README.md;"
            f" branch preserved at {sha[:12]}")
        # The branch sits at its pre-merge sha and the worktree holds no
        # merge in progress -- the abort's refusal did not leave one.
        self.assertEqual(self.git("rev-parse", branch).strip(), sha)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt).strip(), sha)
        self.assertNotEqual(
            subprocess.run(["git", "rev-parse", "-q", "--verify",
                            "MERGE_HEAD"], cwd=wt,
                           capture_output=True).returncode, 0)
        self.assertEqual(holophyte.claim.merge_conflicts(wt), [])
        self.assertEqual(self.git("status", "--porcelain", cwd=wt), "")
        self.assertEqual(
            self.read("SELECT status FROM tickets"),
            [("blocked_on_operator",)])

    def test_a_merge_committed_over_uncommitted_edits_is_rejected(self):
        """A merge commit with uncommitted edits is not a clean candidate."""
        provider = StubProvider(a_task())
        conn, run_id, branch, wt, sha = self.conflicted()

        class CommitMergeLeavingEdits(Commit):
            def play(self, cwd, turn):
                out = super().play(cwd, turn)
                (cwd / self.path).write_text("edited after the merge\n")
                return out

        fake = FakeAgent(CommitMergeLeavingEdits(
            "merge main into the branch", path="README.md",
            body="merged\n"))
        with patch.object(holophyte.loop, "agent", fake):
            with self.assertRaises(holophyte.gates.RunFailure) as failed:
                holophyte.merge_gate._sync_main_into_branch(
                    self.tgt, conn, run_id, provider, "KO-131", branch,
                    wt, sha, 60, "add a thing", 5)

        self.assertEqual(
            str(failed.exception),
            f"merging main into {branch} conflicted on: README.md;"
            f" branch preserved at {sha[:12]}")
        # The turn's merge commit is unwound with the merge: HEAD and the
        # branch are back at the pre-merge sha and the tree is clean.
        self.assertEqual(self.git("rev-parse", branch).strip(), sha)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt).strip(), sha)
        self.assertNotEqual(
            subprocess.run(["git", "rev-parse", "-q", "--verify",
                            "MERGE_HEAD"], cwd=wt,
                           capture_output=True).returncode, 0)
        self.assertEqual(self.git("status", "--porcelain", cwd=wt), "")
        self.assertEqual(
            self.read("SELECT status FROM tickets"),
            [("blocked_on_operator",)])

    def test_a_turn_that_discards_the_candidate_fails_and_parks(self):
        """The turn can end the merge and reset the branch to main: HEAD
        is then clean with main its ancestor, but the candidate's
        pre-merge sha is not -- going on would verify main, not the
        merge, and the branch's work would be lost."""
        provider = StubProvider(a_task())
        conn, run_id, branch, wt, sha = self.conflicted()

        class AbortThenResetToMain:
            role = "implement"

            @staticmethod
            def play(cwd, turn):
                subprocess.run(["git", "merge", "--abort"], cwd=cwd,
                               check=True, capture_output=True)
                subprocess.run(["git", "reset", "--hard", "main"], cwd=cwd,
                               check=True, capture_output=True)
                return "aborted the merge and reset the branch to main"

        with patch.object(holophyte.loop, "agent",
                          FakeAgent(AbortThenResetToMain())):
            with self.assertRaises(holophyte.gates.RunFailure) as failed:
                holophyte.merge_gate._sync_main_into_branch(
                    self.tgt, conn, run_id, provider, "KO-131", branch,
                    wt, sha, 60, "add a thing", 5)

        self.assertEqual(
            str(failed.exception),
            f"merging main into {branch} conflicted on: README.md;"
            f" branch preserved at {sha[:12]}")
        self.assertEqual(self.git("rev-parse", branch).strip(), sha)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt).strip(), sha)
        self.assertNotEqual(
            subprocess.run(["git", "rev-parse", "-q", "--verify",
                            "MERGE_HEAD"], cwd=wt,
                           capture_output=True).returncode, 0)
        self.assertEqual(self.git("status", "--porcelain", cwd=wt), "")
        self.assertEqual(
            self.read("SELECT status FROM tickets"),
            [("blocked_on_operator",)])

    def test_a_resolution_that_times_out_fails_and_parks(self):
        """The turn commits the merge, then the budget kills it: the run
        is over its budget and must not sail on a commit the kill raced --
        `_timed` reports the timeout and the gate fails as before."""
        provider = StubProvider(a_task())
        conn, run_id, branch, wt, sha = self.conflicted()

        class CommitMergeThenTimeout(Commit):
            def play(self, cwd, turn):
                super().play(cwd, turn)
                raise subprocess.TimeoutExpired(
                    "claude", 300, output="resolved, then the cap fired")

        fake = FakeAgent(CommitMergeThenTimeout(
            "merge main into the branch", path="README.md",
            body="merged\n"))
        with patch.object(holophyte.loop, "agent", fake):
            with self.assertRaises(holophyte.gates.RunFailure) as failed:
                holophyte.merge_gate._sync_main_into_branch(
                    self.tgt, conn, run_id, provider, "KO-131", branch,
                    wt, sha, 60, "add a thing", 5)

        self.assertEqual(failed.exception.failure_kind, "budget")
        self.assertEqual(
            str(failed.exception),
            f"merging main into {branch} conflicted on: README.md;"
            f" branch preserved at {sha[:12]}")
        # The turn's merge commit is unwound with the timeout: the branch
        # is back at its pre-merge sha and the tree is clean.
        self.assertEqual(self.git("rev-parse", branch).strip(), sha)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt).strip(), sha)
        self.assertNotEqual(
            subprocess.run(["git", "rev-parse", "-q", "--verify",
                            "MERGE_HEAD"], cwd=wt,
                           capture_output=True).returncode, 0)
        self.assertEqual(self.git("status", "--porcelain", cwd=wt), "")
        self.assertEqual(
            self.read("SELECT status FROM tickets"),
            [("blocked_on_operator",)])

    def test_a_clean_gate_merge_never_calls_the_implementer(self):
        # `main` moved past the branch but on another file, so the merge
        # succeeds on its own and no agent turn is owed.
        branch = "task/ko-131"
        self.git("checkout", "-q", "-b", branch)
        (self.target / "thing.py").write_text("x = 1\n")
        self.git("add", "thing.py")
        self.git("commit", "-qm", "branch side")
        self.git("checkout", "-q", "main")
        (self.target / "README.md").write_text("main\n")
        self.git("commit", "-qam", "main side")
        wt = self.worktrees / "ko-131"
        self.git("worktree", "add", "-q", str(wt), branch)
        sha = self.git("rev-parse", branch, cwd=wt).strip()
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        project = tickets.ensure_project(conn, StubProvider.TEAM,
                                       str(self.target))
        ticket = holophyte.board.mirror_task(conn, project, a_task())
        run_id = store.claim(conn, project, ticket)
        tickets.transition(conn, ticket, "in_flight")
        store.set_branch(conn, run_id, branch)
        fake = FakeAgent()  # no steps: any turn asked for is a ScriptError
        with patch.object(holophyte.loop, "agent", fake):
            merged = holophyte.merge_gate._sync_main_into_branch(
                self.tgt, conn, run_id, StubProvider(a_task()), "KO-131",
                branch, wt, sha, 60, "add a thing", 5)

        self.assertNotEqual(merged, sha)
        self.assertEqual(fake.turns, [])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt).strip(),
                         merged)


class TransportRetryTests(LoopFixture):
    def failed_turn(self, message, code=1):
        class FailedTurn:
            role = "implement"

            def play(inner, cwd, turn):
                # Exercise the real exit-code capture at the agent boundary.
                with patch.object(holophyte.agents, "run_capped",
                                  return_value=(code, message)):
                    return holophyte.agents.agent(
                        self.tgt, "implement", "task", cwd)
        return FailedTurn()

    def test_transport_retry_reaches_review(self):
        elapsed = 0

        def wait(seconds):
            nonlocal elapsed
            elapsed += seconds

        with patch.object(holophyte.loop, "sleep", side_effect=wait) as nap, \
                patch.object(holophyte.loop, "retry_clock",
                             side_effect=lambda: time.monotonic() + elapsed):
            fake, _ = self.loop(self.failed_turn("FETCH FAILED"),
                                Commit("recovered"), APPROVE)
        nap.assert_called_once_with(30)
        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual(self.read("SELECT count(*) FROM runEvents"
                                   " WHERE kind = 'transport_retry'"), [(1,)])
        self.assertEqual(self.read("SELECT payload FROM runEvents"
                                   " WHERE kind = 'implementer_output'"),
                         [("FETCH FAILED",)])
        self.assertLessEqual(fake.turns[1].timeout, fake.turns[0].timeout - 30)

    def test_two_transport_failures_preserve_branch_without_a_strike(self):
        with patch.object(holophyte.loop, "sleep"):
            fake, _ = self.loop(self.failed_turn("ECONNRESET"),
                                self.failed_turn("ECONNRESET"))
        self.assertEqual(fake.roles, ["implement", "implement"])
        ((outcome, kind, reason),) = self.read(
            "SELECT outcome, outcomeClass, outcomeReason FROM runs")
        self.assertEqual((outcome, kind), ("failed", "infra"))
        self.assertIn("ECONNRESET", reason)
        self.assertIn(BRANCH, self.branches())
        self.assertTrue(fake.turns[0].cwd.exists())
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        ((ticket_id,),) = self.read("SELECT ticketId FROM runs")
        self.assertEqual(store.read.failed_attempts_since(conn, ticket_id, 0), [])
        self.assertEqual(self.read("SELECT count(*) FROM runEvents"
                                   " WHERE kind = 'implementer_output'"), [(2,)])

    def test_unrelated_error_and_successful_transport_text_are_work(self):
        for message, code in (("AssertionError", 1), ("fetch failed", 0)):
            with self.subTest(message=message):
                if code == 0:
                    conn = store.open(str(self.db))
                    self.addCleanup(conn.close)
                    tickets.walk_ticket(conn, 1, "ready")
                with patch.object(holophyte.loop, "sleep") as nap:
                    fake, _ = self.loop(self.failed_turn(message, code))
                nap.assert_not_called()
                self.assertEqual(fake.roles, ["implement"])
        self.assertEqual(self.read("SELECT outcomeClass FROM runs"),
                         [("work",), ("work",)])


class NoCommitOutputTests(LoopFixture):
    """A turn that ends without a commit keeps what the implementer said on
    the run (KO-375): the worktree it may have explained itself in is
    discarded, so the event is the operator's only evidence."""

    def events(self):
        return self.read("SELECT summary, payload FROM runEvents"
                         " WHERE kind = 'implementer_output'")

    def removals_seen(self):
        """Wrap the loop's `sh` so the moment the worktree is removed, the
        store is read for the event: the order witness."""
        seen = []
        real = holophyte.loop.sh

        def sh(args, cwd=None):
            if args[:3] == ["git", "worktree", "remove"]:
                seen.append(self.events())
            return real(args, cwd)
        patcher = patch.object(holophyte.loop, "sh", sh)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_a_no_commit_turn_keeps_the_message_before_the_discard(self):
        seen = self.removals_seen()
        message = "Startup noise.\nThe verify line names no file."

        self.loop(Idle(message))

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        self.assertEqual(self.events(),
                         [("The verify line names no file.", message)])
        # Recorded before the removal, not after: the event was already in
        # the store when the worktree went.
        self.assertEqual(seen, [[("The verify line names no file.", message)]])

    def test_a_timed_out_turn_without_commits_keeps_its_output(self):
        """The cap can fire after the implementer has explained itself but
        before it commits: what `agent()` captured before the kill is the
        run's evidence, not an empty payload saying it printed nothing."""
        message = "Startup noise.\nThe verify line names no file."
        seen = self.removals_seen()

        self.loop(IdleThenTimeout(message))
        self.assertEqual(self.read("SELECT failureKind FROM runs"), [("budget",)])

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        self.assertEqual(self.events(),
                         [("The verify line names no file.", message)])
        self.assertEqual(seen, [[("The verify line names no file.", message)]])

    def test_a_nonzero_exit_without_commits_keeps_its_output_too(self):
        """Capture a real CLI's nonzero-exit output before discarding its branch."""
        path = self.db.parent / "implementer.sh"
        path.write_text(
            "#!/bin/sh\n"
            'case "$1" in *ready*) echo ready; exit 0;; esac\n'
            "echo refusing this ticket\necho a second line\nexit 3\n")
        path.chmod(0o755)
        self.configure(f'[agents]\nimplementer = "{path}"\n')
        provider = StubProvider(a_task())
        with no_agent_processes():
            with patch.dict(sys.modules, {"linear_provider": provider}):
                holophyte.operator.main(self.tgt, provider)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(self.events(), [("a second line",
                                          "refusing this ticket\na second line")])

    def test_a_secret_in_prose_output_never_reaches_the_store(self):
        """The review's repro: `redact()` walks TOML and stops at the first
        word of prose, so a credential echoed after a sentence went into
        the store readable. The config's own secret, the board key the
        environment holds, and a pair the loop has no other knowledge of
        are all hidden; the sentence around them stays."""
        self.configure('[agents]\n[linear]\napi_key = "cfg-secret-value"\n')
        message = ("Cannot continue.\n"
                   'api_key = "example-secret-value"\n'
                   "the board answered 401 for cfg-secret-value\n"
                   "and env-secret-value was refused too\n"
                   "GH_TOKEN: ghp_pasted\n"
                   "the token_file path is /run/secrets/x")

        with patch.dict(os.environ, {"LINEAR_API_KEY": "env-secret-value"}):
            self.loop(Idle(message))

        ((summary, payload),) = self.events()
        self.assertEqual(summary, "the token_file path is /run/secrets/x")
        for secret in ("example-secret-value", "cfg-secret-value",
                       "env-secret-value", "ghp_pasted"):
            self.assertNotIn(secret, payload)
        self.assertIn("the board answered 401 for [redacted]", payload)
        self.assertIn("api_key = [redacted]", payload)
        self.assertIn("the token_file path is /run/secrets/x", payload)

    def test_the_payload_keeps_only_the_last_characters_up_to_the_constant(self):
        cap = holophyte.loop.OUTPUT_TAIL
        head = "first line\n"
        message = head + "x" * cap

        self.loop(Idle(message))

        ((summary, payload),) = self.events()
        self.assertEqual(summary, "x" * cap)
        self.assertEqual(len(payload), cap)
        self.assertEqual(payload, message[-cap:])


if __name__ == "__main__":
    unittest.main()


class RunLandingTests(MergeModeFixture):
    def test_local_landing_carries_the_claim(self):
        landing_path(self, "local")

    def test_approved_landing_carries_the_new_claim(self):
        landing_path(self, "approved")

    def test_babysitter_landing_carries_the_claim(self):
        landing_path(self, "pr")
