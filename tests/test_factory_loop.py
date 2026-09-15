"""Factory loop control flow, driven end to end with zero agent calls.

The paths under test are the ones that used to need live agents to exercise:
a clean approval that merges, both review rounds spending their findings and
their fix rounds before a terminal PASS, the two ways adjudication refuses to
merge, and the failure-pattern escalation that stops a ticket the loop keeps
failing on from being claimed again. `tests/fake_agent.py` scripts the agent
turns; everything else is real — a real throwaway repo, real worktrees, the
real verify gate, the real `--no-ff` merge — so what these tests assert is the
loop's behavior and not a model of it.

Run: python3 -m unittest discover -s tests -p 'test_factory_loop*' -v
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
# `fake_agent` is a helper, not a test module: discovery never imports it, and
# how this file is imported decides whether `tests/` is on the path at all.
# Putting it there explicitly makes `discover -s tests` and `-m unittest
# tests.test_factory_loop` resolve the harness the same way.
sys.path.insert(0, str(HERE))
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
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    INVALID_BODY,
    VALID_BODY,
    Boom,
    CommitThenTimeout,
    IdleThenTimeout,
    InfraRefuse,
    Interrupt,
    LoopFixture,
    Refuse,
    StubProvider,
    a_task,
)

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
import holophyte.runs  # noqa: E402 - after the sys.path insert above
import holophyte.supervisor  # noqa: E402 - after the sys.path insert above
import holophyte.target  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets as tickets  # noqa: E402 - after the sys.path insert above


class LoopTests(LoopFixture):
    # --- the clean run ---------------------------------------------------

    def test_a_script_ending_in_approve_merges_without_spawning_an_agent(self):
        """One implement turn, one APPROVE: the branch reaches main, the run
        row says merged, and nothing anywhere under the loop started a real
        agent process."""
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
        """A run approved on its first review still has a `round` entry --
        the approving round -- ahead of its `merge` entry, and each row is in
        the store before the comment that projects it."""
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
        """A findings round, the approving round and the merge each land in
        `ledger` -- in that order, for this run -- and each row is committed
        before the comment that projects it is posted: the provider stub
        reads the table over its own connection at every `comment()` and
        finds the entry already there. The comment is the projection; the
        row is the record."""
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
        """REQUEST_CHANGES twice spends both review rounds and both fix
        rounds, so the loop falls through to the terminal adjudication; a PASS
        there merges, and every fix commit is in main's history."""
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
        """The round cap is the target's `[loop]` review keys applied to
        the candidate's diff (KO-299). With one extra round per changed line
        and a ceiling of 3, a one-line candidate earns a third round, so a
        reviewer that requests changes twice and approves on round 3 merges
        without an adjudicator; the run's row and its narrative carry the
        cap it was given (KO-321). Under the default config the same script
        hits the cap after round 2 and the third turn is the terminal
        adjudication, as today.
        """
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
        """FAIL is terminal and has no fix round: main is untouched, the
        branch and its worktree are left where a human can pick them up, and
        the next queued ticket is never claimed."""
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
        """A reply that names no verdict is not an approval by omission: the
        run fails like an explicit FAIL and the unreadable reply is kept as
        the round's finding."""
        self.loop(Commit("first cut"), REQUEST_CHANGES,
                  Commit("fix round 1"), REQUEST_CHANGES,
                  Commit("fix round 2"), MALFORMED)

        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertIn(BRANCH, self.branches())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(
            self.read("SELECT round, verdict FROM reviewRounds ORDER BY round"),
            [(1, "changes_requested"), (2, "changes_requested"), (3, "error")])

    # --- merge-time drift ------------------------------------------------

    def test_a_ticket_edited_during_the_run_is_not_merged(self):
        """The candidate was implemented, reviewed and verified against the
        ticket as it was claimed. The board now says something else, so the
        approved work answers a contract that no longer exists: main is left
        untouched, the branch and worktree are preserved, and the ticket is
        told which fields moved."""
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
        """A Linear that will not answer is missing evidence, not drift: the
        run says so in its event stream and merges on the contract frozen at
        the claim, because failing closed here would make every outage a
        stuck queue."""
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
        """One failure is not a pattern: the ticket is left in flight rather
        than parked, and once a human walks it back to `ready` the claim path
        lets it through — a second run row is the lease being taken for a
        second attempt, whatever that attempt then runs into."""
        self.fail_once()

        self.assertEqual(self.status(), "in_flight")

        self.fail_again()

        self.assertEqual(self.attempts(), [(1,), (2,)])

    def test_the_second_failure_blocks_the_ticket_and_reports_both_runs(self):
        """At the threshold the ticket stops being open work: the store parks
        it for an operator, the board is told, and one comment accounts for
        every failed run by the reason that run actually ended on."""
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
        """A ticket the store still says is `in_flight` is refused before the
        claim, so no run row is opened for it and no failure is recorded —
        the KO-150 spurious second strike (holophyte-bugs #4) cannot recur.
        The skip is printed with the store status that refused it."""
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

    def test_an_infra_failure_raised_by_the_run_is_closed_out_as_infra(self):
        self.loop(InfraRefuse())

        self.assertEqual(self.rc, 1)
        self.assertEqual(
            self.read("SELECT outcome, outcomeClass, outcomeReason FROM runs"),
            [("failed", "infra", "the reviewer container did not start")])
        self.assertEqual(self.status(), "in_flight")

    def test_infra_failures_alone_never_block_the_ticket(self):
        """Two runs the factory lost on its own are not a pattern about the
        ticket: MAX_FAILED_RUNS of them park nothing."""
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
        """The board says Todo — that is where `blocked_on_operator` projects
        — and offers the ticket back anyway. The claim path refuses it on the
        store's count instead: no run is opened, so no worktree is cut and no
        agent is paid to fail a third time."""
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
        """69fe923's rule stands for board drags — they write no rows and
        forgive nothing — but a *recorded* human intervention is a human
        taking the ticket back: the failures before it are that human's
        accepted history, so one unblock buys a fresh MAX_FAILED_RUNS
        rather than exactly one attempt forever (the KO-146 incident left
        the ticket carrying 4 permanent strikes, none its own fault)."""
        self.fail_once()
        self.fail_again()  # second failure parks it
        self.intervene("human")

        self.fail_once("third")  # first failure since the human acted

        self.assertEqual(self.status(), "in_flight")  # not re-parked

        self.fail_again("fourth")  # second failure since: the pattern is back

        self.assertEqual(self.status(), "blocked_on_operator")

    def test_a_hand_closed_run_is_dispositioned_not_a_carried_strike(self):
        """The canonical repair records the close_out first and releases the
        run a clock-read later; whether the run's endedAt lands before or
        after the row's `at` is jitter, and a run the human dispositioned by
        hand must not be the strike that re-parks the ticket next time."""
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
        """Only a human's recorded touch resets the count: a supervisor
        close-out is the machine talking to itself, and one unblock after
        it still buys exactly one attempt."""
        self.fail_once()
        self.fail_again()
        self.intervene("supervisor")

        self.fail_once("third")

        self.assertEqual(self.status(), "blocked_on_operator")

    def test_a_blocked_ticket_is_skipped_rather_than_stopped_on(self):
        """The blocked ticket keeps its place at the head of the board's ready
        set — `blocked_on_operator` projects to Todo, and the provider offers
        the lowest identifier first — so it is offered ahead of the next
        ticket on this pass and every later one. It is passed over, not
        stopped on: the ticket behind it is claimed, worked and merged in the
        same pass, which is what stops one parked ticket from starving the
        queue behind it forever."""
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
        """`runs.branch` names the task branch before the first `working`
        phase change lands, so a live run's files panel has a worktree to
        read from the moment the run starts implementing."""
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
                        "UPDATE runs SET startedAt = startedAt - ?"
                        " WHERE id = ?", (int(minutes * 60 * 1000), run_id))
            return real(conn, run_id, phase, note)

        return patch.object(holophyte.loop, "set_phase", watching)

    def test_a_fix_turn_the_cap_has_no_room_for_is_refused(self):
        """70 min into a 30 min box, the fix turn's 30 min would take the
        run past 3 boxes: the turn is refused before it starts, the run
        fails naming the minutes, the box, the cap and the preserved sha,
        and the implementer is never invoked for it."""
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
        self.assertIn("min spent of a 30 min box", reason)
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
        """Same run at cap 4: 70 + 30 fits in 120, so the turn starts. The
        cap arithmetic is the only difference -- the fix lands, the next
        review approves, the run merges."""
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


class SweepDiagnosticsTests(LoopFixture):
    """A held ticket and the startup preamble surface the read-only sweep.

    The KO-146 incident's dead end: "lease already held by run 7" with
    nothing about whether run 7 was alive, and no strike recorded, so the
    relaunch reflex never accumulated evidence. One read-only sweep per
    invocation turns the relaunch into the evidence — the second launch can
    act. Since KO-341 the lease is the ticket's, so the held ticket is
    skipped for the next candidate rather than stopping the loop; the line
    still names the holder and points at the sweep.
    """

    MINUTE = 60 * 1000
    HELD = "KO-9"

    def held_task(self):
        """The held ticket as the board would offer it."""
        return dict(a_task(), id=self.HELD, issue_id="iss-stale",
                    title="stalled elsewhere")

    def stale_holder(self, minutes_silent=6, strikes=0):
        """A run some other loop claimed and went silent on, lease held."""
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        store.init(conn)
        project = tickets.ensure_project(conn, StubProvider.TEAM,
                                       str(self.target))
        ticket = tickets.mirror_ticket(
            conn, project, linear_issue_id="iss-stale",
            linear_identifier=self.HELD, title="stalled elsewhere",
            acceptance_criteria=["Given a run, then it heartbeats"],
            verification_commands=["echo ok"], time_box_ms=25 * self.MINUTE)
        tickets.transition(conn, ticket, "in_flight")
        then = int(time.time() * 1000) - minutes_silent * self.MINUTE
        run_id = store.claim(conn, project, ticket, now=then)
        store.set_phase(conn, run_id, "working", now=then)
        if strikes:
            store.record_strike(conn, run_id, True, then, now=then + 1)
        return run_id

    def test_a_held_ticket_prints_the_silence_and_records_a_strike(self):
        run_id = self.stale_holder()

        printed = self.main_output(provider=StubProvider(self.held_task()))

        held = f"ticket {self.HELD}: lease already held by run {run_id}"
        self.assertIn(held, printed)
        # One sweep, printed once: the skip points back at it rather than
        # re-sweeping (double-counting the silence) or reprinting.
        self.assertEqual(printed.count("strike 1 of 2"), 1)
        self.assertLess(printed.index("strike 1 of 2"), printed.index(held))
        self.assertIn("the sweep above", printed)
        self.assertEqual(self.read("SELECT strikes FROM sweepStrikes"),
                         [(1,)])
        # A skip, not a stop: the loop went on to find nothing else ready.
        self.assertIn("no ready tickets", printed)
        self.assertEqual(self.read("SELECT id FROM runs"), [(run_id,)])

    def test_a_startup_sighting_of_a_tripped_run_names_the_acting_sweep(self):
        self.stale_holder(minutes_silent=12, strikes=1)

        printed = self.main_output(provider=StubProvider(self.held_task()))

        self.assertEqual(printed.count("--sweep --act"), 1)
        self.assertIn(str(self.target), printed)  # copy-pasteable hint

    def test_a_healthy_holder_prints_the_skip_and_no_sweep_lines(self):
        """A live run at a fresh heartbeat is swept and found healthy: the
        held line prints alone, with no strike recorded and no table."""
        self.stale_holder(minutes_silent=0)

        printed = self.main_output(provider=StubProvider(self.held_task()))

        self.assertIn("lease already held", printed)
        self.assertNotIn("swept", printed)
        self.assertNotIn("strike", printed)
        self.assertNotIn("the sweep above", printed)
        self.assertEqual(self.read("SELECT strikes FROM sweepStrikes"), [])

    def test_the_loop_skips_the_held_ticket_and_claims_the_next(self):
        """Two loops on one target (KO-341): with KO-9 held elsewhere and
        KO-131 ready behind it, this loop takes KO-131, works it to a merge,
        and says once that KO-9 is held. The holder's run is untouched."""
        run_id = self.stale_holder(minutes_silent=0)
        provider = StubProvider(self.held_task(), a_task())

        out = io.StringIO()
        with patch.object(sys, "stdout", out):
            fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                                provider=provider)
        printed = out.getvalue()

        self.assertEqual(fake.roles, ["implement", "review"])
        held_lines = [line for line in printed.splitlines()
                      if "lease already held" in line]
        self.assertEqual(len(held_lines), 1, printed)
        self.assertIn(f"ticket {self.HELD}: lease already held by run {run_id}",
                      held_lines[0])
        self.assertEqual(
            self.read("SELECT t.linearIdentifier, r.id, r.outcome FROM runs r"
                      " JOIN tickets t ON t.id = r.ticketId ORDER BY r.id"),
            [(self.HELD, run_id, None), ("KO-131", run_id + 1, "merged")])
        self.assertEqual(
            self.read("SELECT linearIdentifier, activeRunId FROM tickets"
                      " ORDER BY linearIdentifier"),
            [("KO-131", None), (self.HELD, run_id)])


class ReconcileTests(LoopFixture):
    """At startup, right after the sweep, the loop walks the mirrored
    tickets Linear has since closed elsewhere to their terminal status.

    The daemon's board showed KO-217 `ready` and KO-137/KO-138 `needs_spec`
    days after another target finished them: the mirror is written at the
    claim and hears nothing when Linear closes the ticket elsewhere.
    """

    CRITERIA = ["Given a thing, when it runs, then it works"]

    def seed(self):
        """Three open mirrored tickets: KO-1 ready and KO-2 needs_spec, each
        with a run behind it (failed, requeued; KO-2's body then lost its
        criteria on a re-mirror), and KO-3 ready and never run."""
        conn = store.open(str(self.db))
        project = tickets.ensure_project(conn, StubProvider.TEAM, str(self.target))
        runs = {}
        for n in (1, 2):
            ticket = tickets.mirror_ticket(
                conn, project, linear_issue_id=f"iss-{n}",
                linear_identifier=f"KO-{n}", title=f"ticket {n}",
                acceptance_criteria=self.CRITERIA,
                verification_commands=["echo ok"])
            runs[f"KO-{n}"] = store.claim(conn, project, ticket)
            tickets.transition(conn, ticket, "in_flight")
            store.release(conn, runs[f"KO-{n}"], "failed", "crashed")
            store.requeue(conn, ticket, "contract fixed")
        tickets.mirror_ticket(conn, project, linear_issue_id="iss-2",
                            linear_identifier="KO-2", title="ticket 2")
        tickets.mirror_ticket(conn, project, linear_issue_id="iss-3",
                            linear_identifier="KO-3", title="ticket 3",
                            acceptance_criteria=self.CRITERIA,
                            verification_commands=["echo ok"])
        conn.close()
        self.assertEqual(self.statuses(), {"KO-1": "ready", "KO-2": "needs_spec",
                                           "KO-3": "ready"})
        return runs

    def statuses(self):
        return dict(self.read("SELECT linearIdentifier, status FROM tickets"))

    def reconcile_rows(self):
        return self.read('SELECT runId, source, "trigger" FROM interventions'
                         " WHERE \"action\" = 'reconcile' ORDER BY id")

    def test_closed_tickets_are_walked_to_their_terminal_status(self):
        runs = self.seed()
        provider = StubProvider()
        provider.closed = {"KO-1": "completed", "KO-2": "canceled"}

        printed = self.main_output(provider=provider)

        self.assertEqual(self.statuses(), {"KO-1": "merged", "KO-2": "abandoned",
                                           "KO-3": "ready"})
        self.assertEqual(self.reconcile_rows(),
                         [(runs["KO-1"], "supervisor", "linear_completed"),
                          (runs["KO-2"], "supervisor", "linear_cancelled")])
        self.assertIn("[holo2] reconciled KO-1: ready -> merged"
                      " (Linear completed)", printed)
        self.assertIn("[holo2] reconciled KO-2: needs_spec -> abandoned"
                      " (Linear canceled)", printed)
        self.assertNotIn("KO-3", printed)
        # One call for the whole open set, KO-3 included.
        self.assertEqual(sorted(provider.asked), ["KO-1", "KO-2", "KO-3"])
        self.assertEqual(provider.states, [])  # nothing is written to Linear

    def test_a_ticket_with_an_active_run_is_left_to_that_run(self):
        conn = store.open(str(self.db))
        project = tickets.ensure_project(conn, StubProvider.TEAM, str(self.target))
        ticket = tickets.mirror_ticket(
            conn, project, linear_issue_id="iss-9", linear_identifier="KO-9",
            title="being worked", acceptance_criteria=self.CRITERIA,
            verification_commands=["echo ok"])
        run_id = store.claim(conn, project, ticket)
        tickets.transition(conn, ticket, "in_flight")
        conn.close()
        provider = StubProvider()
        provider.closed = {"KO-9": "completed"}

        printed = self.main_output(provider=provider)

        self.assertNotIn("reconciled", printed)
        self.assertEqual(self.statuses(), {"KO-9": "in_flight"})
        self.assertEqual(self.read("SELECT id, phase, endedAt FROM runs"),
                         [(run_id, "claimed", None)])
        self.assertEqual(self.reconcile_rows(), [])
        self.assertEqual(getattr(provider, "asked", []), [])

    def test_a_ticket_claimed_while_the_board_is_asked_is_left_to_its_run(self):
        """The active-run check before the provider call is a snapshot:
        another process on the same store can claim the ticket while the
        board is being asked, and the row is re-read under the write lock
        before anything is recorded or walked."""
        self.seed()
        db, team, target = str(self.db), StubProvider.TEAM, str(self.target)

        class ClaimsMeanwhile(StubProvider):
            def closed_identifiers(self, identifiers):
                other = store.open(db)
                project = tickets.ensure_project(other, team, target)
                (ticket_id,) = other.execute(
                    "SELECT id FROM tickets WHERE linearIdentifier = 'KO-1'"
                ).fetchone()
                self.run_id = store.claim(other, project, ticket_id)
                tickets.transition(other, ticket_id, "in_flight")
                other.close()
                return super().closed_identifiers(identifiers)

        provider = ClaimsMeanwhile()
        provider.closed = {"KO-1": "completed", "KO-2": "canceled"}

        printed = self.main_output(provider=provider)

        self.assertEqual(self.statuses(), {"KO-1": "in_flight", "KO-2": "abandoned",
                                           "KO-3": "ready"})
        self.assertEqual(
            self.read("SELECT phase, endedAt FROM runs WHERE id = %d"
                      % provider.run_id), [("claimed", None)])
        self.assertEqual([row[2] for row in self.reconcile_rows()],
                         ["linear_cancelled"])
        self.assertNotIn("reconciled KO-1", printed)
        self.assertIn("reconcile left KO-1 alone", printed)

    def test_only_this_projects_tickets_are_reconciled(self):
        """A store may hold more than one project; the provider knows one
        team, and another project's open tickets are that project's loop
        to reconcile."""
        self.seed()
        conn = store.open(str(self.db))
        other = tickets.ensure_project(conn, "another-team", "/elsewhere")
        tickets.mirror_ticket(conn, other, linear_issue_id="iss-x",
                            linear_identifier="XX-1", title="theirs",
                            acceptance_criteria=self.CRITERIA,
                            verification_commands=["echo ok"])
        conn.close()
        provider = StubProvider()
        provider.closed = {"XX-1": "completed"}

        printed = self.main_output(provider=provider)

        self.assertEqual(self.statuses()["XX-1"], "ready")
        self.assertNotIn("XX-1", provider.asked)
        self.assertNotIn("reconciled", printed)
        self.assertEqual(self.reconcile_rows(), [])

    def test_a_board_that_cannot_answer_skips_the_reconcile_in_one_line(self):
        self.seed()

        class Unreachable(StubProvider):
            def closed_identifiers(self, identifiers):
                raise OSError("network is unreachable")

        printed = self.main_output(Commit("the scripted work"), APPROVE,
                                   provider=Unreachable(a_task()))

        skipped = [line for line in printed.splitlines()
                   if "reconcile skipped" in line]
        self.assertEqual(len(skipped), 1)
        self.assertIn("network is unreachable", skipped[0])
        self.assertNotIn("reconciled", printed)
        self.assertEqual(self.reconcile_rows(), [])
        # The seeded mirror is untouched and the loop went on to claim,
        # implement and merge the queued ticket as before.
        self.assertEqual(self.statuses(),
                         {"KO-1": "ready", "KO-2": "needs_spec", "KO-3": "ready",
                          "KO-131": "merged"})
        self.assertIn("the scripted work", self.subjects())


class QueueMirrorTests(LoopFixture):
    """Each claim mirrors every ready issue the provider lists, so the Board
    shows the queue and not only the ticket the loop picked.

    The operator filed four tickets and saw none of them on the Board: the
    mirror was written at the claim alone, so a ticket in Todo was invisible
    until its turn came.
    """

    def statuses(self):
        return dict(self.read("SELECT linearIdentifier, status FROM tickets"))

    def queue(self):
        """KO-a with a template-valid body, KO-b with the body KO-165 was
        claimed on (the template's Summary placeholder left in, so only the
        validator objects), KO-c valid; the stub offers KO-131 first, so
        that is the one claimed. Every body is a real string so each ticket
        takes the validator's route and not the no-body bypass."""
        a, b, c = a_task(2), a_task(3), a_task(4)
        a["body"] = c["body"] = VALID_BODY
        b["body"] = INVALID_BODY
        return a, b, c

    def head(self):
        """The ticket the claim picks, with a body the validator accepts."""
        return dict(a_task(), body=VALID_BODY)

    def test_one_claim_pass_mirrors_the_whole_ready_listing(self):
        a, b, c = self.queue()
        provider = StubProvider(self.head(), a, b, c)

        self.loop(Boom(), provider=provider)

        # KO-131 ran (and stayed `in_flight` on its failure, as a failed run
        # leaves its ticket); the other three were mirrored, never claimed.
        self.assertEqual(self.statuses(), {"KO-131": "in_flight", "KO-132": "ready",
                                           "KO-133": "needs_spec", "KO-134": "ready"})
        self.assertEqual(self.read("SELECT COUNT(*) FROM runs"), [(1,)])
        # The mirror writes nothing to Linear: the only state posted is the
        # claimed ticket's own In Progress.
        self.assertEqual({issue for issue, _ in provider.states}, {"iss-131"})

    def test_a_ticket_parked_on_the_operator_is_not_moved_by_the_mirror(self):
        a, b, c = self.queue()
        conn = store.open(str(self.db))
        project = tickets.ensure_project(conn, StubProvider.TEAM, str(self.target))
        ticket = holophyte.board.mirror_task(conn, project, c)
        tickets.transition(conn, ticket, "blocked_on_deps")
        tickets.transition(conn, ticket, "blocked_on_operator")
        conn.close()
        provider = StubProvider(self.head(), a, b, c)

        self.loop(Boom(), provider=provider)

        self.assertEqual(self.statuses()["KO-134"], "blocked_on_operator")
        self.assertEqual(self.statuses()["KO-132"], "ready")

    def test_a_listing_failing_after_a_claim_skips_the_mirror_only(self):
        """The board answers the listing before KO-131's claim, KO-131 is
        claimed and merged, and then the board is unreachable when the loop
        comes back to list the queue: the mirror is skipped in one printed
        line and KO-132 is claimed, worked and merged all the same."""
        class ListingFailsAfterTheFirstClaim(StubProvider):
            def __init__(self, *tasks):
                super().__init__(*tasks)
                self.listings = 0

            def ready_issues(self):
                """Answers before the first claim and at the closing pass;
                unreachable exactly once, right after KO-131 merged."""
                self.listings += 1
                if self.listings == 2:
                    raise RuntimeError("board unreachable")
                return super().ready_issues()

        provider = ListingFailsAfterTheFirstClaim(a_task(1), a_task(2))

        printed = self.main_output(Commit("the first work"), APPROVE,
                                   Commit("the second work"), APPROVE,
                                   provider=provider)

        # Both claims proceeded: the one before the failure and the one after.
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("merged",), ("merged",)])
        self.assertIn("the first work", self.subjects())
        self.assertIn("the second work", self.subjects())
        self.assertEqual(provider.queue, [])
        # One failed listing, one skip line, and the claim never waited on it.
        self.assertEqual(provider.listings, 3)
        skipped = [line for line in printed.splitlines()
                   if "queue mirror skipped" in line]
        self.assertEqual(len(skipped), 1, printed)
        self.assertIn("board unreachable", skipped[0])
        self.assertEqual(self.statuses(), {"KO-131": "merged", "KO-132": "merged"})


class CrashContainmentTests(LoopFixture):
    """Any exception out of `run_task()` is that run's failure: closed out
    with the error text as its reason, both leases released, one clean line,
    nonzero exit — never a traceback with the reason lost to the generic
    close-out default (KO-146 incident, run 9)."""

    def leases(self):
        return self.read("SELECT p.activeRunId, t.activeRunId"
                         " FROM projects p, tickets t")

    def test_a_crash_fails_the_run_with_the_error_text_as_its_reason(self):
        self.loop(Boom())

        self.assertEqual(self.rc, 1)
        ((outcome, reason),) = self.read(
            "SELECT outcome, outcomeReason FROM runs")
        self.assertEqual(outcome, "failed")
        self.assertIn("RuntimeError", reason)
        self.assertIn("fatal: scripted", reason)
        self.assertNotIn("\n", reason)  # one line, escalation-comment safe
        self.assertEqual(self.leases(), [(None, None)])

    def test_a_run_failure_carries_its_exact_reason(self):
        self.loop(Refuse())

        self.assertEqual(self.rc, 1)
        self.assertEqual(self.read("SELECT outcome, outcomeReason FROM runs"),
                         [("failed", "some reason")])

    def test_a_keyboard_interrupt_still_propagates_after_the_close_out(self):
        with self.assertRaises(KeyboardInterrupt):
            self.loop(Interrupt())

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(self.leases(), [(None, None)])


class CrashReasonTests(LoopFixture):
    """A crashed run's reason names the frame it escaped from, and its
    traceback is an event: run 103 (KO-273) crashed `database is locked`
    with nothing to say which write raised it."""

    LOCKED = sqlite3.OperationalError("database is locked")

    def crash_in_record_round(self):
        """A run whose review round's store write raises the way a locked
        store does, from inside `holophyte/runs.py`: the innermost frame in
        the factory's own code is `record_round`, the write's caller."""
        with patch.object(store, "record_review_round",
                          side_effect=self.LOCKED):
            return self.main_output(Commit("the scripted work"), APPROVE)

    def test_reason_names_the_factory_frame(self):
        out = self.crash_in_record_round()

        self.assertEqual(self.rc, 1)
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertRegex(reason, r"^OperationalError: database is locked"
                                 r" \(at holophyte/runs\.py:record_round:\d+\)$")
        self.assertIn(f"[holo2] run crashed: {reason}", out)

    def test_traceback_is_an_event(self):
        self.crash_in_record_round()

        rows = self.read("SELECT seq, level, kind, summary, payload"
                         " FROM runEvents ORDER BY seq")
        crashes = [row for row in rows if row[2] == "crash"]
        self.assertEqual(len(crashes), 1)
        seq, level, _, summary, payload = crashes[0]
        self.assertEqual(level, "detail")
        self.assertEqual(
            summary, self.read("SELECT outcomeReason FROM runs")[0][0])
        self.assertIn("Traceback (most recent call last)", payload)
        self.assertIn("OperationalError: database is locked", payload)
        self.assertIn("record_round", payload)
        (failed_seq,) = [row[0] for row in rows
                         if row[2] == "phase_change" and "-> failed" in row[3]]
        self.assertLess(seq, failed_seq)

    def test_no_factory_frame_keeps_the_plain_reason(self):
        """The close-out's reason for a traceback with no frame under the
        repository is the one-line form as before. Through `crash_reason()`
        directly: an exception the loop catches has always passed through the
        loop's own frame, so a traceback with none is one the loop cannot
        produce for itself."""
        try:
            json.loads("{")
        except ValueError as err:
            # Drop this test's own frame; what is left is the standard
            # library's `json` package and nothing else.
            e = err.with_traceback(err.__traceback__.tb_next)

        reason = holophyte.loop.crash_reason(e)

        self.assertTrue(reason.startswith("JSONDecodeError: Expecting"), reason)
        self.assertNotIn("(at ", reason)
        self.assertNotIn("\n", reason)


class StopOnFailureTests(LoopFixture):
    """`[loop] stop_on_failure`: whether one failed run ends the whole pass.

    The default is the loop as it always was — a failure is closed out and
    the process exits nonzero with the next ticket unclaimed. `false` is the
    unattended night: the same close-out, then the next ready ticket in the
    same process. Escalation is not this knob's business, and the tests keep
    to one failure per ticket so it never enters.
    """

    def outcomes(self):
        return self.read(
            "SELECT t.linearIdentifier, r.outcome FROM runs r"
            " JOIN tickets t ON t.id = r.ticketId ORDER BY r.id")

    def test_by_default_one_failure_stops_the_pass_with_the_next_unclaimed(self):
        provider = StubProvider(a_task(1), a_task(2))

        self.loop(Refuse(), provider=provider)

        self.assertEqual(self.rc, 1)
        self.assertEqual(self.outcomes(), [("KO-131", "failed")])
        self.assertEqual([t["id"] for t in provider.queue], ["KO-132"])

    def goes_on_past(self, failure):
        """`stop_on_failure = false`, two tickets, the first run lost to
        `failure`: the run is closed out as today — lease back, ticket left
        in flight for a human — and then the second ticket is claimed,
        worked and merged in the same process. The pass still exits nonzero
        so a shell sees the night was not clean."""
        self.configure("[loop]\nstop_on_failure = false\n")
        provider = StubProvider(a_task(1), a_task(2))

        out = self.main_output(failure, Commit("the other work"), APPROVE,
                               provider=provider)

        self.assertEqual(self.outcomes(),
                         [("KO-131", "failed"), ("KO-132", "merged")])
        self.assertIn("the other work", self.subjects())
        self.assertEqual(provider.queue, [])
        self.assertIn("continuing to the next ready ticket", out)
        self.assertEqual(self.read("SELECT status FROM tickets ORDER BY id"),
                         [("in_flight",), ("merged",)])
        self.assertEqual(self.read("SELECT activeRunId FROM projects"),
                         [(None,)])
        self.assertEqual(self.rc, 1)

    # One test per failure exit `main()` has: a `RunFailure`, an
    # `InfraFailure`, and a contained crash.
    def test_false_claims_the_next_ticket_after_a_run_failure(self):
        self.goes_on_past(Refuse())

    def test_false_claims_the_next_ticket_after_an_infra_failure(self):
        self.goes_on_past(InfraRefuse())

    def test_false_claims_the_next_ticket_after_a_crash(self):
        self.goes_on_past(Boom())


class MergeApprovalTests(LoopFixture):
    """`[merge] approve = "human"`: an approved, verified candidate stops at
    the gate for a person to say "merge", and the loop goes on. `"auto"`,
    or no table, merges as it always has."""

    def test_human_parks_the_approved_run_and_blocks_the_ticket(self):
        """The reviewer approved and the pre-merge verify passed, so this is
        the moment the loop used to merge: instead main is untouched, the
        branch and worktree stay, the run is parked alive in
        `awaiting_merge_approval` -- no ending, no outcome -- with its lease
        released, the ticket is `blocked_on_operator` asking `merge?`, and
        the ledger names the branch and candidate sha."""
        self.configure('[merge]\napprove = "human"\n')
        provider = StubProvider(a_task())

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            provider=provider)

        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertIn(BRANCH, self.branches())
        self.assertIn("the scripted work", self.subjects(BRANCH))
        self.assertTrue((self.worktrees / "ko-131-add-a-thing").exists())
        self.assertEqual(self.transitions()[-2:],
                         ["reviewing -> merge_gate",
                          "merge_gate -> awaiting_merge_approval"])
        self.assertEqual(
            self.read("SELECT phase, endedAt, outcome FROM runs"),
            [("awaiting_merge_approval", None, None)])
        self.assertEqual(self.read("SELECT candidateSha FROM runs"),
                         [(self.git("rev-parse", BRANCH).strip(),)])
        self.assertEqual(self.read("SELECT activeRunId FROM projects"),
                         [(None,)])
        self.assertEqual(
            self.read("SELECT activeRunId, lastRunId FROM tickets"),
            [(None, 1)])
        self.assertEqual(
            self.read("SELECT status, blockedQuestion FROM tickets"),
            [("blocked_on_operator", "merge?")])
        sha = self.git("rev-parse", BRANCH).strip()
        # The approving round's comment precedes the parking notice.
        (_, body) = provider.comments[-1]
        self.assertIn("AWAITING MERGE APPROVAL", body)
        self.assertIn(BRANCH, body)
        self.assertIn(sha, body)

    def test_a_parked_run_is_listed_by_attention_under_blocked(self):
        """What the operator sees: `/attention` reads the parked ticket
        straight out of the store, under `blocked`, with `merge?`."""
        self.configure('[merge]\napprove = "human"\n')

        self.loop(Commit("the scripted work"), APPROVE)

        import holophyte.serve
        code, body = holophyte.serve.attention(self.tgt)
        self.assertEqual(code, 200)
        blocked = [item for item in body["items"] if item["kind"] == "blocked"]
        # The item names the parked run; this path records no `redirect`
        # intervention, so `asked_ms` is the run's last heartbeat.
        (beat,), = self.read("SELECT lastHeartbeat FROM runs WHERE id = 1")
        self.assertEqual(blocked, [{"kind": "blocked", "ticket": "KO-131",
                                    "question": "merge?", "run": 1,
                                    "asked_ms": beat, "pr_url": None,
                                    "level": "attention"}])

    def test_a_park_is_not_a_failure_the_loop_stops_on_or_counts(self):
        """The loop moves on to the next ready ticket without spending the
        stop or the exit status, and the park is not a strike: the ticket's
        failure history stays empty."""
        self.configure('[merge]\napprove = "human"\n')
        provider = StubProvider(a_task(1), a_task(2))

        out = self.main_output(Commit("first"), APPROVE,
                               Commit("second"), APPROVE, provider=provider)

        self.assertIsNone(self.rc)
        self.assertEqual(provider.queue, [])
        self.assertIn("KO-131 parked awaiting merge approval", out)
        self.assertEqual(
            self.read("SELECT t.linearIdentifier, t.status FROM tickets t"
                      " ORDER BY t.id"),
            [("KO-131", "blocked_on_operator"),
             ("KO-132", "blocked_on_operator")])
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        for (ticket_id,) in conn.execute("SELECT id FROM tickets"):
            self.assertEqual(holophyte.board.failure_history(conn, ticket_id),
                             [])

    def test_approve_then_the_next_claim_merges_the_candidate_unreviewed(self):
        """`--approve KO-n` releases the parked run, and the loop's next
        claim takes the preserved candidate straight to the gate: no
        implementer or reviewer turn (an empty script would raise on any),
        the pre-merge verify runs again, the candidate lands on main
        `--no-ff`, the new run walks `claimed -> merge_gate -> merging ->
        done` and is marked merged, and the parked run is over with its
        resume point recorded."""
        self.configure('[merge]\napprove = "human"\n')
        marker = self.worktrees.parent / "verified"
        task = dict(a_task(), verify=f"touch {marker}")
        self.loop(Commit("the scripted work"), APPROVE,
                  provider=StubProvider(dict(task)))
        marker.unlink()
        out = io.StringIO()
        holophyte.operator.approve(self.tgt, "KO-131", "ok", out=out)
        self.assertIn("KO-131 approved: run 1", out.getvalue())

        fake, guard = self.loop(provider=StubProvider(dict(task)))

        self.assertEqual(fake.roles, [])
        self.assertEqual(guard.spawned, [])
        self.assertTrue(marker.exists(), "the pre-merge verify did not run")
        self.assertIn("the scripted work", self.subjects())
        self.assertNotIn(BRANCH, self.branches())
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        self.assertEqual(
            self.read("SELECT id, phase, outcome, resumePhase FROM runs"
                      " ORDER BY id"),
            [(1, "failed", "abandoned", "merge_gate"),
             (2, "done", "merged", None)])
        self.assertEqual(
            [summary.split(":")[0] for (summary,) in
             self.read("SELECT summary FROM runEvents WHERE runId = 2"
                       " AND kind = 'phase_change' ORDER BY seq")],
            ["claimed -> merge_gate", "merge_gate -> merging",
             "merging -> done"])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])
        self.assertEqual(self.read("SELECT activeRunId FROM projects"),
                         [(None,)])

    def test_the_resumed_run_records_its_branch_before_the_gate(self):
        """The resumed run reuses the candidate's worktree rather than
        cutting one, and still names the branch before its first phase
        change: the files panel reads `runs.branch` to find the worktree
        on this path as on a fresh cut."""
        self.configure('[merge]\napprove = "human"\n')
        self.loop(Commit("the scripted work"), APPROVE)
        holophyte.operator.approve(self.tgt, "KO-131", "ok", out=io.StringIO())
        seen = []
        real = holophyte.merge_gate.set_phase

        def watching(conn, run_id, phase, note=None):
            (branch,) = conn.execute(
                "SELECT branch FROM runs WHERE id = ?", (run_id,)).fetchone()
            seen.append((run_id, phase, branch))
            return real(conn, run_id, phase, note)

        with patch.object(holophyte.merge_gate, "set_phase", watching):
            self.loop()

        self.assertEqual(seen[0], (2, "merge_gate", BRANCH))
        self.assertEqual(
            self.read("SELECT id, branch, outcome FROM runs ORDER BY id"),
            [(1, BRANCH, "abandoned"), (2, BRANCH, "merged")])

    def test_a_babysitter_release_of_a_local_park_does_not_merge(self):
        """`--babysit` is not an approval. `store.babysit()` refuses a run
        parked with no pull request, but the resumed claim holds the line
        on its own: a parked local candidate whose newest intervention is
        `babysit` (written here through the store API, the way an operator
        at the REPL rung could) is not taken through the gate -- the next
        run fails naming the release, main is untouched, the branch and
        worktree stay for `--approve`."""
        self.configure('[merge]\nmode = "local"\napprove = "human"\n')
        self.loop(Commit("the scripted work"), APPROVE,
                  provider=StubProvider(a_task()))
        with self.assertRaises(SystemExit) as refused:
            holophyte.operator.babysit_ticket(self.tgt, "KO-131", "look again",
                                           out=io.StringIO())
        self.assertIn("no pull request", str(refused.exception))
        conn = holophyte.runs.open_store(self.tgt)
        try:
            store.record_intervention(conn, 1, "babysit", "look again")
            store.release(conn, 1, "abandoned", "released by hand")
            conn.execute("UPDATE runs SET resumePhase = 'merge_gate'"
                         " WHERE id = 1")
            tickets.walk_ticket(conn, 1, "ready")
            conn.commit()
        finally:
            conn.close()

        fake, _ = self.loop(provider=StubProvider(a_task()))

        self.assertEqual(fake.roles, [])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertIn(BRANCH, self.branches())
        self.assertTrue((self.worktrees / "ko-131-add-a-thing").exists())
        rows = self.read("SELECT id, phase, outcome, outcomeReason FROM runs"
                         " ORDER BY id")
        self.assertEqual([row[:3] for row in rows],
                         [(1, "failed", "abandoned"), (2, "failed", "failed")])
        self.assertIn("released by --babysit", rows[1][3])
        self.assertNotIn("merged", [s for (s,) in
                                    self.read("SELECT status FROM tickets")])

    def test_a_candidate_changed_since_the_park_is_refused_at_the_gate(self):
        """An approval is of the sha the reviewer approved and the pre-merge
        verify passed. A worktree that no longer sits on it -- a commit
        added since the park, or uncommitted edits -- is not that candidate:
        the next claim fails without merging, without preserving the edits
        as a WIP commit, and with the branch and worktree left in place for
        a human, naming the sha it expected."""
        for tamper in ("commit", "dirty"):
            with self.subTest(tamper=tamper):
                self.setUp()
                self.configure('[merge]\napprove = "human"\n')
                wt = self.worktrees / "ko-131-add-a-thing"
                self.loop(Commit("the scripted work"), APPROVE)
                approved = self.git("rev-parse", "HEAD", cwd=wt).strip()
                holophyte.operator.approve(self.tgt, "KO-131", "ok",
                                       out=io.StringIO())
                (wt / "later.txt").write_text("added after the approval\n")
                if tamper == "commit":
                    self.git("add", "-A", cwd=wt)
                    self.git("-c", "user.name=t", "-c", "user.email=t@x",
                             "commit", "-m", "slipped in after approval",
                             cwd=wt)

                fake, _ = self.loop()

                self.assertEqual(fake.roles, [])
                self.assertEqual(self.git("rev-parse", "main").strip(),
                                 self.base)
                self.assertNotIn("the scripted work", self.subjects())
                self.assertIn(BRANCH, self.branches())
                self.assertTrue(wt.exists())
                self.assertNotIn("WIP", self.subjects(BRANCH))
                (run_id, outcome, reason) = self.read(
                    "SELECT id, outcome, outcomeReason FROM runs"
                    " ORDER BY id DESC LIMIT 1")[0]
                self.assertEqual((run_id, outcome), (2, "failed"))
                self.assertIn(approved[:12], reason)

    def test_a_remote_commit_past_the_approved_candidate_is_not_merged(self):
        """The approved candidate's resume reuses the worktree too, and an
        `origin` the branch is shared with can hold commits past the
        approval. The reclaim's fetch-and-fast-forward (KO-410) must not run
        on this path: the approval is of the sha the park recorded, so the
        resume merges exactly that sha and the remote's commit stays on
        origin — before the fix the fast-forward moved the branch onto it
        and the gate merged it with no review."""
        self.configure('[merge]\napprove = "human"\n')
        self.loop(Commit("the scripted work"), APPROVE)
        wt = self.worktrees / "ko-131-add-a-thing"
        approved = self.git("rev-parse", "HEAD", cwd=wt).strip()
        # The remote copy of the parked branch, moved one commit on by a
        # person — published the way LeftoverWorktreeTests publishes.
        bare = self.worktrees.parent / "origin.git"
        self.git("init", "-q", "--bare", str(bare))
        self.git("remote", "add", "origin", str(bare))
        self.git("fetch", "-q", str(self.target), f"{BRANCH}:{BRANCH}",
                 cwd=bare)
        clone = self.worktrees.parent / "person"
        self.git("clone", "-q", "-b", BRANCH, str(bare), str(clone))
        self.git("config", "user.email", "person@example.invalid", cwd=clone)
        self.git("config", "user.name", "A Person", cwd=clone)
        (clone / "person.txt").write_text("unreviewed\n")
        self.git("add", "-A", cwd=clone)
        self.git("commit", "-q", "-m", "person: pushed past the approval",
                 cwd=clone)
        theirs = self.git("rev-parse", "HEAD", cwd=clone).strip()
        self.git("fetch", "-q", str(clone), f"{BRANCH}:{BRANCH}", cwd=bare)
        holophyte.operator.approve(self.tgt, "KO-131", "ok", out=io.StringIO())

        fake, _ = self.loop()

        self.assertEqual(fake.roles, [])
        # The merge's second parent is the approved sha, not the remote's:
        # the person's commit is on `main` nowhere.
        self.assertEqual(self.git("rev-parse", "main^2").strip(), approved)
        self.assertNotIn(theirs, self.git("rev-list", "main").split())
        self.assertIn("the scripted work", self.subjects())
        self.assertEqual(
            self.read("SELECT outcome FROM runs WHERE id = 2"),
            [("merged",)])
        self.assertEqual(
            self.read("SELECT text FROM ledger WHERE text LIKE"
                      " 'Fast-forwarded%'"), [])

    def test_auto_and_an_absent_table_merge_as_before(self):
        for toml in ('[merge]\napprove = "auto"\n', None):
            with self.subTest(config=toml):
                self.setUp()
                if toml is not None:
                    self.configure(toml)

                self.loop(Commit("the scripted work"), APPROVE)

                self.assertIn("the scripted work", self.subjects())
                self.assertNotIn(BRANCH, self.branches())
                self.assertEqual(self.read("SELECT outcome FROM runs"),
                                 [("merged",)])
                self.assertEqual(self.read("SELECT status FROM tickets"),
                                 [("merged",)])


class MergeAfterTests(LoopFixture):
    """`[merge] after` (KO-347): the target's commands run in the checkout
    once the merge has landed; a failing one parks the run with its output
    and leaves the merge commit on main."""

    def test_the_commands_run_in_the_checkout_and_the_run_merges(self):
        self.configure('[merge]\nafter = ["sh -c \'touch after.ran\'"]\n')

        out = self.main_output(Commit("the scripted work"), APPROVE)

        self.assertTrue((self.target / "after.ran").exists())
        self.assertIn("[holo2] after: sh -c 'touch after.ran' -> exit 0", out)
        self.assertIn("the scripted work", self.subjects())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])

    def test_a_failing_command_parks_the_run_and_keeps_the_merge(self):
        """The merge stands -- main holds the work -- but the run is not
        marked merged: it parks `blocked_on_operator`, alive and lease
        released, with the command's output in the note and the ticket's
        question; the second command never runs."""
        self.configure('[merge]\nafter = ["sh -c \'echo boom >&2; exit 1\'",'
                       ' "sh -c \'touch after.ran\'"]\n')
        provider = StubProvider(a_task())

        out = self.main_output(Commit("the scripted work"), APPROVE,
                               provider=provider)

        self.assertIn("the scripted work", self.subjects())
        self.assertNotEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertFalse((self.target / "after.ran").exists())
        self.assertIn("-> exit 1", out)
        self.assertEqual(
            self.read("SELECT phase, endedAt, outcome FROM runs"),
            [("blocked_on_operator", None, None)])
        self.assertEqual(self.read("SELECT activeRunId FROM projects"),
                         [(None,)])
        ((status, question),) = self.read(
            "SELECT status, blockedQuestion FROM tickets")
        self.assertEqual(status, "blocked_on_operator")
        self.assertIn("boom", question)
        ((note,),) = self.read(
            "SELECT summary FROM runEvents WHERE kind = 'phase_change'"
            " ORDER BY id DESC LIMIT 1")
        self.assertIn("boom", note)
        (_, body) = provider.comments[-1]
        self.assertIn("MERGED to main", body)
        self.assertIn("boom", body)


class SelfHostingTests(LoopFixture):
    """A loop working on the factory's own repository re-executes itself
    after a merge, so the merged code is what runs the next pass. Through
    the `EXEC` seam: the test runner is never exec-ed."""

    def setUp(self):
        super().setUp()
        self.execs = []
        patcher = patch.object(holophyte.operator, "EXEC",
                               lambda *args: self.execs.append(args))
        patcher.start()
        self.addCleanup(patcher.stop)

    def host_the_factory_in(self, repo):
        """Make the module look imported from `repo`, the way it is when the
        target is the factory's own checkout."""
        patcher = patch.object(holophyte.operator, "__file__",
                               str(repo / "holophyte" / "operator.py"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_merge_into_the_factory_itself_re_executes_the_loop(self):
        # The original command line, interpreter flags included: a loop
        # launched with -u must keep streaming its log after the restart.
        orig = ["/usr/bin/python3", "-u", "factory.py", "/repos/holophyte"]
        with patch.object(sys, "orig_argv", orig):
            self.host_the_factory_in(self.target)
            out = self.main_output(Commit("the scripted work"), APPROVE)

        self.assertEqual(self.execs, [("/usr/bin/python3", orig)])
        head = self.git("rev-parse", "--short", "HEAD").strip()
        self.assertIn("merged a change to the factory itself;"
                      f" re-executing from {head}: {orig}", out)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_the_re_exec_leaves_a_restart_note_the_sweep_can_watch(self):
        """Before the exec, not after: a re-exec that dies prints nothing,
        so the note is the only witness. It names the merged sha and stands
        unreturned until a loop claims or exits clean."""
        orig = ["/usr/bin/python3", "factory.py", "/repos/holophyte"]
        with patch.object(sys, "orig_argv", orig):
            self.host_the_factory_in(self.target)
            self.loop(Commit("the scripted work"), APPROVE)

        head = self.git("rev-parse", "--short", "HEAD").strip()
        self.assertEqual(
            self.read("SELECT sha, returnedAt, reportedAt FROM loopRestarts"),
            [(head, None, None)])

    def test_re_exec_resolves_a_bare_interpreter_name_on_path(self):
        """`sys.orig_argv[0]` is whatever the operator typed -- usually the
        bare `python3` -- and `os.execv` does not search PATH: the first live
        re-exec on the writer host died with FileNotFoundError on exactly that. The
        program handed to the exec must be a real path; argv stays verbatim."""
        orig = ["python3", "-u", "factory.py", "/repos/holophyte"]
        with patch.object(sys, "orig_argv", orig):
            self.host_the_factory_in(self.target)
            self.loop(Commit("the scripted work"), APPROVE)

        ((program, argv),) = self.execs
        self.assertEqual(program, shutil.which("python3"))
        self.assertTrue(os.path.isabs(program), program)
        self.assertEqual(argv, orig)

    def test_re_exec_falls_back_to_executable_and_argv_without_orig_argv(self):
        self.host_the_factory_in(self.target)
        with patch.object(sys, "orig_argv", []):
            self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(self.execs,
                         [(sys.executable, [sys.executable, *sys.argv])])

    def test_self_hosted_is_the_repository_that_holds_the_package(self):
        """Unpatched: the module lives in `holophyte/`, one level below the
        repository it is compared against, so the answer must be about the
        repository -- a loop on the factory's own checkout re-execs -- and
        not about the package directory, which is never a target."""
        holo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, holo, ignore_errors=True)

        def target(path):
            return holophyte.target.Target(
                path=path, holo_dir=holo, store_path=holo / "store.db",
                config_path=holo / "config.toml", worktrees=holo / "wt")

        self.assertTrue(holophyte.operator.self_hosted(target(ROOT)))
        self.assertFalse(holophyte.operator.self_hosted(target(ROOT / "holophyte")))

    def test_a_merge_into_another_repository_does_not_re_execute(self):
        self.host_the_factory_in(self.target.parent / "elsewhere")
        self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual(self.execs, [])

    def test_a_failed_self_hosted_run_stops_without_re_executing(self):
        self.host_the_factory_in(self.target)
        provider = StubProvider(a_task(1), a_task(2))
        self.loop(Commit("first cut"), REQUEST_CHANGES,
                  Commit("fix round 1"), REQUEST_CHANGES,
                  Commit("fix round 2"), FAIL, provider=provider)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(self.rc, 1)
        self.assertEqual(len(provider.queue), 1)  # the loop stopped
        self.assertEqual(self.execs, [])


if __name__ == "__main__":
    unittest.main()


class HeartbeatTests(LoopFixture):
    """The loop beats while an agent runs, so a slow agent is not a dead loop.

    Run 39 (KO-212) was failed by the supervisor's stale-heartbeat sweep while
    its implementer was still working: the heartbeat moved only at phase
    boundaries. Here the implementer turn blocks for longer than the whole
    stale budget, `heartbeat_stale_min * stale_strikes`, and sweeps the store
    from inside its wait the way the supervisor would.
    """

    def test_an_implementer_slower_than_the_stale_budget_is_not_tripped(self):
        # 0.01 min is 600 ms; two strikes make a 1.2 s budget. The turn
        # below sweeps every 400 ms for 2 s.
        self.configure("[supervisor]\nheartbeat_stale_min = 0.01\n")
        knobs = holophyte.config_tables.sweep_config(self.tgt)
        budget_s = knobs.heartbeat_stale_ms * knobs.stale_strikes / 1000
        db, tgt = self.db, self.tgt
        sightings = []

        class SlowCommit(Commit):
            """An implementer that works past the stale budget, sweeping the
            store as it goes, then commits like `Commit`."""

            def play(self, cwd, turn):
                conn = store.open(str(db))
                try:
                    deadline = time.monotonic() + budget_s * 5 / 3
                    while time.monotonic() < deadline:
                        time.sleep(0.4)
                        result = holophyte.supervisor.sweep(
                            tgt, conn, int(time.time() * 1000), knobs=knobs)
                        sightings.append((
                            result.trips,
                            conn.execute("SELECT phase, lastHeartbeat FROM"
                                         " runs").fetchone()))
                finally:
                    conn.close()
                return super().play(cwd, turn)

        fake, guard = self.loop(SlowCommit("the slow work"), APPROVE)

        self.assertEqual(guard.spawned, [])
        self.assertGreaterEqual(len(sightings), 4, sightings)
        self.assertEqual([trips for trips, _ in sightings if trips], [])
        # The heartbeat moved during the turn while the phase did not: the
        # beat, not a stage boundary, kept the run alive.
        phases = {phase for _, (phase, _) in sightings}
        beats = [beat for _, (_, beat) in sightings]
        self.assertEqual(phases, {"working"})
        self.assertGreater(beats[-1], beats[0])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertIn("the slow work", self.subjects())


class EndedRunTests(LoopFixture):
    """A run the store has ended cannot advance (KO-213).

    Run 39 was failed by the supervisor sweep while its implementer ran; when
    the implementer returned, the loop walked the ended run `failed ->
    verifying -> reviewing` and was heading for a merge under a row that said
    the work had failed. Here the implementer turn ends its own run through
    `store.release()` -- the sweep's write, from another connection, the
    way `act_on_trip()` makes it -- and commits as usual. The turn returns
    before the next timer beat, so it is the heartbeat block's exit beat
    (KO-339) that finds the end: the loop stops the turn and moves on.
    """

    def test_a_run_failed_mid_agent_stops_with_the_sweeps_verdict(self):
        db = self.db
        sweep_reason = "supervisor sweep: stale_heartbeat (2 strikes); failing"

        class SweptCommit(Commit):
            def play(self, cwd, turn):
                conn = store.open(str(db))
                try:
                    (run_id,) = conn.execute("SELECT id FROM runs").fetchone()
                    store.release(conn, run_id, "failed", sweep_reason)
                finally:
                    conn.close()
                return super().play(cwd, turn)

        out = self.main_output(SweptCommit("swept work"), APPROVE)
        provider = self.last_provider

        self.assertIn("[holo2] run 1 was ended by the supervisor"
                      f" ({sweep_reason}); stopping this turn", out)
        # The stream ends where the sweep ended it: no phase event after the
        # release, so nothing reanimated the run.
        self.assertEqual(self.transitions(),
                         ["claimed -> working", "working -> failed"])
        self.assertEqual(
            self.read("SELECT outcome, outcomeReason, phase FROM runs"),
            [("failed", sweep_reason, "failed")])
        # Nothing pushed to the board past the claim, nothing merged, and
        # the loop went on to its next claim rather than stopping.
        self.assertEqual(provider.states, [("iss-131", "In Progress")])
        self.assertEqual(self.subjects(), ["base"])
        self.assertIn("Linear has no ready tickets. done.", out)
        self.assertIsNone(self.rc)
        # The worktree and its branch are as the implementer left them.
        self.assertIn("swept work", self.subjects("task/ko-131-add-a-thing"))
        self.assertTrue(any(p.is_dir() for p in self.worktrees.iterdir()))
        # The reviewer never ran: the script's APPROVE is still unconsumed.
        self.assertEqual([turn.role for turn in self.last_fake.turns],
                         ["implement"])


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
        """The turn commits the merge but leaves edits behind: the sha is
        a merge, yet the tree the gate's verify would read is not the one
        the sha holds, so it does not count and the run parks as before."""
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


class FindingsModeTests(LoopFixture):
    """KO-363: `[report] findings` decides whether a close-out renders
    FINDINGS.md into the target at all. The store is the record; the file
    is a projection a target opts into with `"repo"`.
    """

    def findings_commits(self):
        """Subjects of the commits on main that touched FINDINGS.md."""
        return self.git("log", "main", "--format=%s", "--",
                        "FINDINGS.md").splitlines()

    def dirt(self):
        """What `git status` sees in the target checkout after the run."""
        return self.git("status", "--porcelain").strip()

    def test_a_target_with_no_findings_key_merges_without_the_file(self):
        """The default: the run merges, main holds the `--no-ff` merge
        commit and nothing above it, the checkout has no FINDINGS.md and
        no commit ever touched one."""
        self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual(self.subjects()[0],
                         "Merge task/ko-131-add-a-thing: add a thing")
        self.assertFalse((self.target / "FINDINGS.md").exists())
        self.assertEqual(self.findings_commits(), [])
        self.assertEqual(self.dirt(), "")

    def test_a_target_that_opts_in_has_the_window_rendered_and_committed(self):
        """`findings = "repo"`: the close-out renders the window over the
        store's rows and commits it on main above the merge, as before."""
        self.configure('[report]\nfindings = "repo"\n')

        self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        rendered = (self.target / "FINDINGS.md").read_text()
        self.assertIn(holophyte.findings.FINDINGS_MARKER, rendered)
        self.assertIn("KO-131", rendered)
        self.assertEqual(self.findings_commits(),
                         ["Complete task KO-131: add a thing"])
        self.assertEqual(self.subjects()[:2],
                         ["Complete task KO-131: add a thing",
                          "Merge task/ko-131-add-a-thing: add a thing"])
        self.assertEqual(self.dirt(), "")


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
        message = "This contract cannot be met.\nThe verify line names no file."

        self.loop(Idle(message))

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        self.assertEqual(self.events(),
                         [("This contract cannot be met.", message)])
        # Recorded before the removal, not after: the event was already in
        # the store when the worktree went.
        self.assertEqual(seen, [[("This contract cannot be met.", message)]])

    def test_a_timed_out_turn_without_commits_keeps_its_output(self):
        """The cap can fire after the implementer has explained itself but
        before it commits: what `agent()` captured before the kill is the
        run's evidence, not an empty payload saying it printed nothing."""
        message = "This contract cannot be met.\nThe verify line names no file."
        seen = self.removals_seen()

        self.loop(IdleThenTimeout(message))

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        self.assertEqual(self.events(),
                         [("This contract cannot be met.", message)])
        self.assertEqual(seen, [[("This contract cannot be met.", message)]])

    def test_a_nonzero_exit_without_commits_keeps_its_output_too(self):
        """A real route, not the fake: the turn's exit code is the process's,
        so this is the loop reading a failed harness's last words."""
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
        self.assertEqual(self.events(), [("refusing this ticket",
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
        self.assertEqual(summary, "Cannot continue.")
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
        self.assertEqual(summary, "first line")
        self.assertEqual(len(payload), cap)
        self.assertEqual(payload, message[-cap:])


if __name__ == "__main__":
    unittest.main()
