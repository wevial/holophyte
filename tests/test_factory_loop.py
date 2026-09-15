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
import re
import shlex
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
    Reply,
    no_agent_processes,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    INVALID_BODY,
    TICK,
    VALID_BODY,
    Boom,
    CommitThenTimeout,
    FakePool,
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
        real = holophyte.loop.set_phase

        def watching(conn, run_id, phase, note=None):
            (branch,) = conn.execute(
                "SELECT branch FROM runs WHERE id = ?", (run_id,)).fetchone()
            seen.append((run_id, phase, branch))
            return real(conn, run_id, phase, note)

        with patch.object(holophyte.loop, "set_phase", watching):
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


class MergeModeFixture(LoopFixture):
    """`[merge] mode = "pr"`'s fixture: an approved, verified candidate is
    pushed and opened as a pull request instead of merged, and the loop
    babysits the PR -- threads verdicted, fixed and answered, checks
    awaited -- until it merges through the PR's API or the run parks.
    `"local"`, or no key, merges as it always has.

    `git` and `gh` on PATH are fakes that record their argv: the fake `git`
    intercepts `push` alone and hands everything else to the real one, so
    the loop's worktrees, merges and rev-parses are real while the one call
    that would leave the machine is witnessed instead of made. The fake
    `gh` answers `pr create` with `URL` and `api` with what the test put in
    the state files: the PR's threads and checks for the state query, an
    empty success for the reply and resolve mutations, `MERGE_SHA` for the
    merge.

    Split from the tests so a suite elsewhere -- the conflicting-PR tests
    in `test_babysitter.py` -- drives the same fake GitHub without
    re-running the tests that came with it."""

    URL = "https://github.com/example/repo/pull/7"
    # The `origin` the fixture target is given: the repository the push
    # goes to and the one `gh pr create` must be pinned to.
    ORIGIN = "https://github.com/example/repo.git"
    MERGE_SHA = "9f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"
    # A body the claim-time template gate accepts, so the run reaches the
    # gate with a ticket body for the PR to carry.
    BODY = VALID_BODY
    # Two threads a review bot might leave: a clear defect and a style nit.
    DEFECT = ("src/app.py", 10, "review-bot",
              "`load()` returns None when the file is missing and the"
              " caller indexes it: a crash on first run.")
    NIT = ("src/app.py", 20, "style-bot",
           "Prefer `thing_count` over `n` for this variable name.")

    @staticmethod
    def comment(number, author, body):
        """One comment as the GraphQL answer carries it. `author` is a
        login -- a review bot's, `__typename` `Bot`, the kind the babysitter
        answers -- or a `(login, typename)` pair for a person (`User`)."""
        login, kind = (author if isinstance(author, tuple)
                       else (author, "Bot"))
        return {"author": {"login": login, "__typename": kind},
                "body": body,
                "url": f"{MergeModeFixture.URL}#discussion_r{number}"}

    @classmethod
    def thread(cls, number, path, line, author, body, replies=(),
               resolved=False, next_cursor=None):
        """One review thread as the GraphQL answer carries it: the opening
        comment, then `replies` (each `(author, body)`) as the follow-ups
        on its first page of comments; `next_cursor` names a further page
        the babysitter must fetch."""
        nodes = [cls.comment(number, author, body)]
        nodes += [cls.comment(f"{number}_{n}", who, text)
                  for n, (who, text) in enumerate(replies, 1)]
        return {"id": f"PRRT_{number}", "isResolved": resolved,
                "isOutdated": False, "path": path, "line": line,
                "comments": {"pageInfo": {"hasNextPage": next_cursor
                                          is not None,
                                          "endCursor": next_cursor},
                             "nodes": nodes}}

    @classmethod
    def comments_page(cls, number, replies, next_cursor=None):
        """A later page of one thread's comments, as the thread query
        answers it."""
        return {"data": {"node": {"comments": {
            "pageInfo": {"hasNextPage": next_cursor is not None,
                         "endCursor": next_cursor},
            "nodes": [cls.comment(f"{number}_p{n}", who, text)
                      for n, (who, text) in enumerate(replies, 1)]}}}}

    # What the fake `gh` swaps for the branch's real tip when it serves a
    # state: the PR's head is the candidate the loop pushed, unless a test
    # says otherwise (`head=`).
    HEAD = "HEAD_SHA"

    def pr_state(self, threads=(), checks="SUCCESS", merged=False,
                 head=HEAD, resolved=(), next_cursor=None,
                 mergeable="MERGEABLE"):
        """The state query's answer: `threads` (each a `DEFECT`/`NIT`-shaped
        tuple) open, `resolved` the same shape but resolved, the head's
        check rollup, whether the PR is merged, GitHub's `mergeable`
        answer (None for the lazy-computation `null`), and -- for a page
        that is not the last -- the cursor of the next."""
        nodes = [self.thread(n, *t) for n, t in enumerate(threads, 1)]
        nodes += [self.thread(n, *t[:4], resolved=True)
                  for n, t in enumerate(resolved, len(nodes) + 1)]
        return {"data": {"repository": {"pullRequest": {
            "state": "MERGED" if merged else "OPEN", "merged": merged,
            "headRefOid": head, "mergeable": mergeable,
            "mergeCommit": {"oid": self.MERGE_SHA} if merged else None,
            "commits": {"nodes": [{"commit": {"statusCheckRollup":
                                              {"state": checks}}}]},
            "reviewThreads": {
                "pageInfo": {"hasNextPage": next_cursor is not None,
                             "endCursor": next_cursor},
                "nodes": nodes}}}}}

    def fake_route(self, push_exit=0, push_sh="", states=None,
                   comments=(), open_pr=None):
        """Put a recording `git` and `gh` ahead of the real PATH, and give
        the target an `origin` for them to name.

        Each call appends its argv to `self.calls`; `gh pr create` keeps
        the body it read on stdin in `self.pr_body` and prints `URL`; each
        `gh api` call keeps its JSON body under `self.api_dir` (read back
        by `api_calls()`) and answers by what the body asks: the state
        query gets the first of `states` (each served once until the last,
        which is served forever), a mutation an empty success, the merge
        `MERGE_SHA`, a thread's further comments page the next of
        `comments` (each a `comments_page()`), the open step's
        `pullRequests(headRefName:)` lookup (KO-407) one open pull request
        at `open_pr` -- none without it -- and the reconcile's pull-status
        read (KO-359) an open pull request; the check-runs and
        branch-rules reads answer no runs and no rules, so the rollup
        alone decides the checks. `push_exit` is what `git
        push` answers with --
        non-zero is a remote refusing -- and `push_sh` is shell the fake
        push runs first, for a push that takes its time. A push the fake
        answers successfully also appends `REF SHA` to `self.push_log`:
        the refspec's source resolved in the pushing checkout at push
        time, which is the tip a real remote's branch would have
        received (`pushed()` reads it back).
        """
        self.git("remote", "add", "origin", self.ORIGIN)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bindir = Path(tmp.name)
        self.calls = bindir / "calls.log"
        self.pr_body = bindir / "pr_body.md"
        self.push_log = bindir / "pushes.log"
        self.api_dir = bindir / "api"
        self.api_dir.mkdir()
        # The open step's lookup answer: `open_pr` is the URL the branch
        # is already open as, None the common "no open pull request".
        self.open_answer = bindir / "open.json"
        nodes = [{"url": open_pr}] if open_pr else []
        self.open_answer.write_text(json.dumps(
            {"data": {"repository":
                      {"pullRequests": {"nodes": nodes}}}}))
        answers = bindir / "states"
        answers.mkdir()
        for n, state in enumerate([self.pr_state()] if states is None
                                  else states, 1):
            (answers / f"{n:03d}.json").write_text(json.dumps(state))
        # Kept on the fixture so `serve()` can hand a resumed run a fresh
        # answer sequence mid-test without re-faking PATH.
        self.answers = answers
        pages = bindir / "comments"
        pages.mkdir()
        for n, page in enumerate(comments, 1):
            (pages / f"{n:03d}.json").write_text(json.dumps(page))
        real_git = shutil.which("git")
        # The fetch before every cut (KO-378) is `git fetch origin` with
        # no refspec and would ask the example remote for real; the fake
        # route answers just that call as an origin with nothing new,
        # unrecorded: `self.calls` witnesses what the loop sends out
        # (pushes, pull requests), and a fetch sends nothing. A fetch
        # with a refspec is a different caller — the babysit resume's
        # `fetch origin BRANCH` and the fixture's fetches into a bare
        # remote — and reaches the real git, which fails against the
        # example remote or succeeds against a bare one as it would.
        (bindir / "git").write_text(
            "#!/bin/sh\n"
            'if [ "$1" = fetch ] && [ "$#" = 2 ] && [ "$2" = origin ];'
            " then exit 0; fi\n"
            'if [ "$1" = push ]; then\n'
            f'  printf "git %s\\n" "$*" >> "{self.calls}"\n'
            f"{push_sh}\n"
            f'  if [ {push_exit} -ne 0 ]; then\n'
            '    echo "remote: refused" >&2\n'
            f"    exit {push_exit}\n"
            "  fi\n"
            # The push is witnessed, not made; what a real remote's
            # branch would have received is the refspec's source
            # resolved now, in the pushing checkout.
            '  for src in "$@"; do :; done\n'
            '  src="${src%%:*}"; src="${src#+}"\n'
            f'  printf "%s %s\\n" "$src" "$("{real_git}" rev-parse'
            f' "$src" 2>/dev/null || echo MISSING)" >> "{self.push_log}"\n'
            "  exit 0\n"
            "fi\n"
            f'exec "{real_git}" "$@"\n')
        (bindir / "gh").write_text(
            "#!/bin/sh\n"
            f'printf "gh %s\\n" "$*" >> "{self.calls}"\n'
            'if [ "$1" = api ]; then\n'
            '  case "$*" in\n'
            '    *check-runs*) echo \'{"check_runs":[]}\'; exit 0;;\n'
            '    *rules/branches/*) echo \'[]\'; exit 0;;\n'
            '  esac\n'
            f'  n=$(ls "{self.api_dir}" | wc -l); n=$((n+1))\n'
            f'  body="{self.api_dir}/$n.json"; cat > "$body"\n'
            '  if grep -q resolveReviewThread "$body"; then\n'
            "    echo '{\"data\":{\"resolveReviewThread\":{}}}'\n"
            '  elif grep -q addPullRequestReviewThreadReply "$body"; then\n'
            "    echo '{\"data\":{\"addPullRequestReviewThreadReply\":{}}}'\n"
            '  elif grep -q mergedBy "$body"; then\n'
            "    echo '{\"data\":{\"repository\":{\"pullRequest\":"
            "{\"state\":\"OPEN\",\"merged\":false}}}}'\n"
            '  elif grep -q PullRequestReviewThread "$body"; then\n'
            f'    f=$(ls "{pages}"/*.json | head -1); cat "$f"; rm "$f"\n'
            '  elif grep -q headRefName "$body"; then\n'
            f'    cat "{self.open_answer}"\n'
            '  elif grep -q reviewThreads "$body"; then\n'
            f'    f=$(ls "{answers}"/*.json | head -1)\n'
            f'    tip=$("{real_git}" -C "{self.target}" rev-parse {BRANCH})\n'
            f'    sed "s/{self.HEAD}/$tip/" "$f"\n'
            f'    [ $(ls "{answers}"/*.json | wc -l) -gt 1 ] && rm "$f"\n'
            "  else\n"
            f"    echo '{{\"sha\":\"{self.MERGE_SHA}\",\"merged\":true}}'\n"
            "  fi\n"
            "  exit 0\n"
            "fi\n"
            f'cat > "{self.pr_body}"\n'
            f"echo {self.URL}\n")
        for script in ("git", "gh"):
            (bindir / script).chmod(0o755)
        patcher = patch.dict(os.environ,
                             {"PATH": f"{bindir}:{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def recorded(self):
        return (self.calls.read_text().splitlines()
                if self.calls.exists() else [])

    def pushed(self):
        """Every `git push` the fake answered, as `(ref, sha)`: the
        refspec's source resolved in the pushing checkout at push time --
        the tip a real remote's branch would have received, which is the
        witness a bare argv count cannot give."""
        return [tuple(line.split())
                for line in (self.push_log.read_text().splitlines()
                             if self.push_log.exists() else [])]

    def serve(self, *states):
        """Replace the state answers the fake `gh` still owes with `states`
        -- served in order, the last one sticky -- so a run resumed
        mid-test reads what GitHub now says. The calls log is untouched:
        the pushes and requests already witnessed keep counting."""
        n = max((int(p.stem) for p in self.answers.iterdir()), default=0)
        for p in self.answers.iterdir():
            p.unlink()
        for k, state in enumerate(states or (self.pr_state(),), n + 1):
            (self.answers / f"{k:03d}.json").write_text(json.dumps(state))

    def api_calls(self):
        """Every `gh api` body the babysitter made, in order, as `(kind,
        variables)`: the kind is `state`, `reply`, `resolve` or `merge`.
        The loop's per-pass pull-status read of a parked run (KO-359) is
        left out: it is the reconcile's, tested on its own below, and
        every pass after a park makes one. The open step's
        `pullRequests(headRefName:)` lookup (KO-407) is left out too: it
        is the open step's, not the pass's, and is witnessed by
        `recorded()` and the api bodies instead."""
        calls = []
        for path in sorted(self.api_dir.iterdir(),
                           key=lambda p: int(p.stem)):
            body = json.loads(path.read_text())
            query = body.get("query", "")
            if "mergedBy" in query or "headRefName" in query:
                continue
            kind = ("resolve" if "resolveReviewThread" in query
                    else "reply" if "addPullRequestReviewThreadReply" in query
                    else "comments" if "PullRequestReviewThread" in query
                    else "state" if "reviewThreads" in query else "merge")
            calls.append((kind, body.get("variables", body)))
        return calls

    def provider(self):
        return StubProvider(dict(a_task(), body=self.BODY))

    def question(self):
        ((status, question),) = self.read(
            "SELECT status, blockedQuestion FROM tickets")
        self.assertEqual(status, "blocked_on_operator")
        return question


class MergeModeTests(MergeModeFixture):
    """The `[merge] mode = "pr"` tests: push and open, the passes over
    threads and checks, the parks and resumes, the merge through the pull
    request's API. The conflicting-PR merge-in has its own suite beside
    the texts it shares a module with (`test_babysitter.py`)."""

    def test_pr_pushes_opens_the_pull_request_and_parks_the_run(self):
        """Push, then create, in that order; the PR is titled `KO-n: TITLE`
        and its body is the ticket body followed by the run's FINDINGS
        entry; the babysitter's one pass finds no thread and green checks,
        and under `approve = "human"` the run parks "ready to merge": the
        URL `gh` printed is the run's `prUrl` and heads the ticket's
        question; main is untouched, the branch and worktree stay, and the
        run is parked alive in `awaiting_merge_approval` with its lease
        released -- the `approve = "human"` park, with a URL."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        provider = self.provider()

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            provider=provider)

        self.assertEqual(fake.roles, ["implement", "review"])
        calls = self.recorded()
        # The seventh is the park reading the pull request once more, after
        # the pass's own writes, for the activity mark it records (KO-362);
        # the eighth is the pass after the park asking GitHub whether the
        # parked pull request has been merged (KO-359).
        self.assertEqual(len(calls), 8, calls)
        self.assertEqual(calls[6:], ["gh api --hostname github.com --method"
                                     " POST graphql --input -"] * 2)
        self.assertEqual(calls[0], f"git push origin {BRANCH}")
        # Between the push and the create, the open step's lookup of an
        # open pull request on the branch (KO-407) -- answered none here.
        self.assertEqual(calls[1], "gh api --hostname github.com --method"
                                   " POST graphql --input -")
        # Beside the state query: the head's check runs and main's rules,
        # so a rollup that says success before the checks have reported is
        # not read as green.
        tip = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(calls[4:6], [
            "gh api --hostname github.com --method GET"
            f" repos/example/repo/commits/{tip}/check-runs?per_page=100",
            "gh api --hostname github.com --method GET"
            " repos/example/repo/rules/branches/main"])
        # Pinned to the repository the push went to, not `gh`'s own default
        # repository (`gh repo set-default`), which can point elsewhere.
        self.assertEqual(
            calls[2],
            f"gh pr create --repo {self.ORIGIN} --base main --head {BRANCH}"
            " --title KO-131: add a thing --body-file -")
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        body = self.pr_body.read_text()
        self.assertIn("The thing, added.", body)
        self.assertIn("— KO-131", body)  # the FINDINGS entry heading
        self.assertIn("estimate: 5 min · rounds: 1", body)
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertIn(BRANCH, self.branches())
        self.assertTrue((self.worktrees / "ko-131-add-a-thing").exists())
        self.assertEqual(
            self.read("SELECT phase, endedAt, outcome, prUrl, candidateSha"
                      " FROM runs"),
            [("awaiting_merge_approval", None, None, self.URL,
              self.git("rev-parse", BRANCH).strip())])
        self.assertEqual(self.read("SELECT activeRunId FROM projects"),
                         [(None,)])
        question = self.question()
        self.assertTrue(question.startswith(f"PR open: {self.URL}\n"),
                        question)
        self.assertIn("ready to merge", question)
        (_, comment) = provider.comments[-1]
        self.assertIn("PR OPEN", comment)
        self.assertIn(self.URL, comment)
        # The pass is a round of the run, stamped as the checks' pass.
        self.assertEqual(
            self.read("SELECT round, verdict, reviewerModel FROM reviewRounds"
                      " ORDER BY round")[-1],
            (2, "pass", "github:ci"))

    def test_an_open_pull_request_on_the_branch_is_adopted_not_created(self):
        """KO-407: a run resumed on a branch its failed predecessor left
        open as a pull request -- the requeue scenario -- must not call
        `gh pr create`: GitHub refuses a second open PR for one head, and
        the run used to fail after doing everything right. The open step
        asks GitHub first; a hit is adopted -- `runs.prUrl` is that PR --
        and the run goes straight into a babysit pass, which here finds
        green checks and no threads and parks "ready to merge"."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        adopted = "https://github.com/example/repo/pull/2177"
        self.fake_route(open_pr=adopted)
        provider = self.provider()

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            provider=provider)

        self.assertEqual(fake.roles, ["implement", "review"])
        calls = self.recorded()
        self.assertEqual(calls[0], f"git push origin {BRANCH}")
        # The lookup, between the push and where the create would be --
        # and no `gh pr create` follows it.
        self.assertEqual(calls[1], "gh api --hostname github.com --method"
                                   " POST graphql --input -")
        body = json.loads((self.api_dir / "1.json").read_text())
        self.assertIn("headRefName", body["query"])
        self.assertIn("states: OPEN", body["query"])
        self.assertEqual(body["variables"],
                         {"owner": "example", "name": "repo",
                          "branch": BRANCH})
        self.assertFalse(any(c.startswith("gh pr create") for c in calls),
                         calls)
        # The adopted PR is babysat like an opened one: the state read is
        # the pass's, the round is stamped, and the park names the PR.
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        self.assertEqual(
            self.read("SELECT phase, prUrl, candidateSha FROM runs"),
            [("awaiting_merge_approval", adopted,
              self.git("rev-parse", BRANCH).strip())])
        question = self.question()
        self.assertTrue(question.startswith(f"PR open: {adopted}\n"),
                        question)
        self.assertIn("ready to merge", question)
        (_, comment) = provider.comments[-1]
        self.assertIn("PR OPEN", comment)
        self.assertIn(adopted, comment)
        self.assertEqual(
            self.read("SELECT verdict, reviewerModel FROM reviewRounds"
                      " ORDER BY round")[-1],
            ("pass", "github:ci"))

    def test_an_open_pull_request_is_adopted_through_a_slashed_origin(self):
        """An `origin` ending in `/` -- `https://github.com/example/repo/`
        is a URL `git remote add` accepts -- must still reach GitHub:
        `_origin_pull()` gluing `/pull/0` onto the slash would hand
        `PR_URL_RE` a doubled slash it refuses, the lookup would return
        None without asking, and `gh pr create` would fire and fail just
        as before KO-407. With the slash normalized the branch's open PR
        is found and adopted."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        adopted = "https://github.com/example/repo/pull/2177"
        self.fake_route(open_pr=adopted)
        self.git("remote", "set-url", "origin",
                 "https://github.com/example/repo/")

        self.loop(Commit("the scripted work"), APPROVE,
                  provider=self.provider())

        calls = self.recorded()
        self.assertEqual(calls[0], f"git push origin {BRANCH}")
        self.assertEqual(calls[1], "gh api --hostname github.com --method"
                                   " POST graphql --input -")
        body = json.loads((self.api_dir / "1.json").read_text())
        self.assertEqual(body["variables"],
                         {"owner": "example", "name": "repo",
                          "branch": BRANCH})
        self.assertFalse(any(c.startswith("gh pr create") for c in calls),
                         calls)
        self.assertEqual(self.read("SELECT prUrl FROM runs"), [(adopted,)])

    def test_no_open_pull_request_on_the_branch_opens_one_as_today(self):
        """The lookup answering no open pull request for the branch: the
        push and the lookup run, then `gh pr create` opens the PR exactly
        as before (KO-407)."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()  # open_pr=None: no open pull request

        self.loop(Commit("the scripted work"), APPROVE,
                  provider=self.provider())

        calls = self.recorded()
        self.assertEqual(calls[0], f"git push origin {BRANCH}")
        self.assertEqual(calls[1], "gh api --hostname github.com --method"
                                   " POST graphql --input -")
        self.assertEqual(
            calls[2],
            f"gh pr create --repo {self.ORIGIN} --base main --head {BRANCH}"
            " --title KO-131: add a thing --body-file -")
        self.assertEqual(self.read("SELECT prUrl FROM runs"),
                         [(self.URL,)])

    AGENTS_MD = ("# Agent guide\n\nTitle starts with [Feature Name]."
                 " No testing plan.\n")
    WRITTEN = Idle("Reading the diff.\n"
                   "TITLE: [Contacts] Put Contact Name first\n\n"
                   "The two forms now ask for the contact's name before"
                   " anything else.\n\nThe thing file is what changed.\n")

    def written_target(self):
        """`pr_text = "written"` with a style line, and an `AGENTS.md` on
        main for the worktree to carry."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       'pr_text = "written"\n'
                       'pr_style = "No ticket identifier in the title."\n')
        (self.target / "AGENTS.md").write_text(self.AGENTS_MD)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "agent guide")
        self.base = self.git("rev-parse", "main").strip()
        self.fake_route()
        return StubProvider(dict(
            a_task(), body=self.BODY,
            url="https://linear.app/example/issue/KO-131/add-a-thing"))

    def test_a_ticket_parked_on_a_pr_is_skipped_by_its_url(self):
        """The park keeps the ticket in Todo, so the next pass is offered it
        first. The skip line names the pull request and the `--approve`
        that merges it -- not a failure -- and the ticket behind it is
        claimed and merged in the same pass."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        self.loop(Commit("the scripted work"), APPROVE,
                  provider=self.provider())
        self.assertEqual(self.question().split("\n")[0],
                         f"PR open: {self.URL}")
        # The pass after the park, merging locally so the second ticket's
        # own path is not this test's subject.
        self.configure("")
        parked, other = a_task(), dict(a_task(2), title="add another thing")

        out = self.main_output(Commit("the other work"), APPROVE,
                               provider=StubProvider(parked, other))

        self.assertIn(f"[holo2] KO-131 is parked on PR {self.URL} awaiting"
                      " --approve KO-131; skipping it\n", out)
        self.assertNotIn("failures", out)
        self.assertIn("the other work", self.subjects())
        self.assertEqual(
            self.read("SELECT linearIdentifier, status FROM tickets"
                      " ORDER BY id"),
            [("KO-131", "blocked_on_operator"), ("KO-132", "merged")])

    def test_a_written_pr_takes_the_turns_title_and_body(self):
        """`pr_text = "written"`: after the approval one more implementer
        turn is given the diff, the ticket, the repository's `AGENTS.md`
        and the style line; the PR is created with the title it answered
        and a body ending with the Linear line, with no FINDINGS entry."""
        provider = self.written_target()

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            self.WRITTEN, provider=provider)

        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        prompt = fake.turns[2].goal
        self.assertEqual(fake.turns[2].cwd,
                         self.worktrees / "ko-131-add-a-thing")
        self.assertIn("+the scripted work", prompt)  # the diff
        self.assertIn("The thing, added.", prompt)  # the ticket
        self.assertIn(self.AGENTS_MD.strip(), prompt)
        self.assertIn("No ticket identifier in the title.", prompt)
        # A small budget from the run's remaining box, never the whole run.
        self.assertTrue(60 <= fake.turns[2].timeout <= 5 * 60,
                        fake.turns[2].timeout)
        create = [c for c in self.recorded() if c.startswith("gh pr create")]
        self.assertEqual(create, [
            f"gh pr create --repo {self.ORIGIN} --base main --head {BRANCH}"
            " --title [Contacts] Put Contact Name first --body-file -"])
        body = self.pr_body.read_text()
        self.assertTrue(body.startswith(
            "The two forms now ask for the contact's name"), body)
        self.assertEqual(
            body.rstrip().splitlines()[-1],
            "Linear: KO-131 (https://linear.app/example/issue/KO-131/"
            "add-a-thing)")
        self.assertNotIn("\u2014 KO-131", body)  # no FINDINGS entry heading
        self.assertNotIn("estimate: 5 min", body)
        self.assertNotIn("Reading the diff.", body)
        self.assertEqual(
            self.read("SELECT phase, prUrl FROM runs"),
            [("awaiting_merge_approval", self.URL)])

    def test_a_reply_without_a_title_falls_back_to_the_ticket_form(self):
        """No `TITLE:` line: the PR is still opened, titled `KO-n: TITLE`
        with the ticket body and the FINDINGS entry, and one printed line
        says the written text was refused."""
        provider = self.written_target()

        out = self.main_output(
            Commit("the scripted work"), APPROVE,
            Idle("I would call this [Contacts] Put Contact Name first.\n\n"
                 "The two forms now \u2026"),
            provider=provider)

        create = [c for c in self.recorded() if c.startswith("gh pr create")]
        self.assertEqual(create, [
            f"gh pr create --repo {self.ORIGIN} --base main --head {BRANCH}"
            " --title KO-131: add a thing --body-file -"])
        body = self.pr_body.read_text()
        self.assertIn("The thing, added.", body)
        self.assertIn("\u2014 KO-131", body)
        refused = [line for line in out.splitlines()
                   if "written PR text refused" in line]
        self.assertEqual(len(refused), 1, out)
        self.assertIn("KO-131", refused[0])
        self.assertIn("TITLE:", refused[0])
        self.assertEqual(
            self.read("SELECT phase, prUrl FROM runs"),
            [("awaiting_merge_approval", self.URL)])

    def test_a_squash_only_repository_merges_with_its_configured_method(self):
        """`[merge] pr_merge_method = "squash"`: the one `PUT
        .../pulls/7/merge` carries `merge_method` `squash`, still pinned to
        the approved candidate, and the run records the sha GitHub answered
        -- for a squash, the new commit on `main`, not a merge commit."""
        self.configure('[merge]\nmode = "pr"\npr_merge_method = "squash"\n')
        self.fake_route()

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            provider=self.provider())

        self.assertEqual(self.api_calls()[-1],
                         ("merge", {"merge_method": "squash",
                                    "sha": fake.turns[1].candidate_sha}))
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("done", "merged", self.MERGE_SHA)])

    def test_a_slow_push_keeps_the_run_heartbeating(self):
        """The push and the create block for as long as the remote takes,
        outside any agent turn or verify: a push longer than the stale
        budget was a `stale_heartbeat` trip for the supervisor, which could
        fail the run before its URL was recorded. The fake push here samples
        the run's `lastHeartbeat` from the store while it takes longer than
        the whole stale budget; the beat must move under it."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       "[supervisor]\nheartbeat_stale_min = 0.01\n")
        knobs = holophyte.config_tables.sweep_config(self.tgt)
        budget_s = knobs.heartbeat_stale_ms * knobs.stale_strikes / 1000
        samples = self.db.parent / "heartbeats.log"
        sampler = (
            "import sqlite3, sys, time\n"
            f"deadline = time.monotonic() + {budget_s * 5 / 3}\n"
            f"conn = sqlite3.connect({str(self.db)!r})\n"
            "while time.monotonic() < deadline:\n"
            "    time.sleep(0.2)\n"
            "    row = conn.execute('SELECT phase, lastHeartbeat FROM runs')"
            ".fetchone()\n"
            f"    open({str(samples)!r}, 'a').write('%s %s\\n' % row)\n")
        self.fake_route(push_sh=f"  {sys.executable} -c {shlex.quote(sampler)}")

        self.loop(Commit("the scripted work"), APPROVE,
                  provider=self.provider())

        seen = [line.split() for line in samples.read_text().splitlines()]
        self.assertGreaterEqual(len(seen), 4, seen)
        self.assertEqual({phase for phase, _ in seen}, {"merge_gate"})
        beats = [int(beat) for _, beat in seen]
        self.assertGreater(beats[-1], beats[0])
        # No gap between beats reached the stale threshold.
        self.assertLess(max(b - a for a, b in zip(beats, beats[1:])),
                        knobs.heartbeat_stale_ms)
        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl FROM runs"),
            [("awaiting_merge_approval", None, self.URL)])

    def test_a_green_quiet_pr_under_auto_merges_through_the_api(self):
        """Acceptance: zero unresolved threads and green checks with
        `approve = "auto"`: the PR is merged through the merge API -- one
        `PUT .../pulls/7/merge`, never a local merge or a push of main --
        and the run is marked merged with the sha GitHub answered; the
        worktree and local branch are cleaned up, local main untouched."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route()
        provider = self.provider()

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            provider=provider)

        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertEqual(self.api_calls(),
                         [("state", {"owner": "example", "name": "repo",
                                     "number": 7, "after": None}),
                          # Pinned to the candidate the reviewer approved.
                          ("merge", {"merge_method": "merge",
                                     "sha": fake.turns[1].candidate_sha})])
        self.assertIn("gh api --hostname github.com --method PUT"
                      " repos/example/repo/pulls/7/merge --input -",
                      self.recorded())
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [f"git push origin {BRANCH}"])
        # Local main is untouched: the candidate landed on GitHub's main,
        # and the close-out renders no FINDINGS.md by default (KO-363).
        self.assertEqual(self.subjects(), ["base"])
        self.assertNotIn(BRANCH, self.branches())
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha, prUrl FROM runs"),
            [("done", "merged", self.MERGE_SHA, None)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])
        (_, comment) = provider.comments[-1]
        self.assertIn(f"MERGED through {self.URL} as {self.MERGE_SHA}",
                      comment)
        # The persisted ledger line for a pass with nothing to answer opens
        # the way every pass does, so the console reads one shape (KO-373).
        ((ledger,),) = self.read(
            "SELECT text FROM ledger WHERE kind = 'round' AND text LIKE"
            " 'Babysit pass%'")
        self.assertTrue(ledger.startswith(f"Babysit pass 1 over {self.URL}"),
                        ledger)

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

    def test_a_check_runs_read_the_babysitter_cannot_make_is_pending(self):
        """`pr_state()` reads the head's check runs beside the rollup; a
        read that raises leaves `checks` pending -- never green on a
        rollup alone -- and the exception does not escape the read."""
        def raising_rest(target, pull, method, path, payload=None):
            raise holophyte.pr.InfraFailure(f"GitHub refused GET {path}")
        pull = holophyte.pr.parse_pr_url(self.URL)
        with patch.object(holophyte.pr, "graphql",
                          lambda *a, **k: self.pr_state(checks="SUCCESS")
                          ["data"]), \
                patch.object(holophyte.pr, "rest", raising_rest):
            state = holophyte.pr.pr_state(self.tgt, pull)

        self.assertEqual(state.checks, "pending")
        self.assertEqual(state.head_sha, self.HEAD)

    def _state_with_rest(self, rest):
        pull = holophyte.pr.parse_pr_url(self.URL)
        with patch.object(holophyte.pr, "graphql",
                          lambda *a, **k: self.pr_state(checks="SUCCESS")
                          ["data"]), \
                patch.object(holophyte.pr, "rest", rest):
            return holophyte.pr.pr_state(self.tgt, pull)

    def test_check_runs_are_read_to_the_last_page_before_green(self):
        """Review finding: only the first page of check runs was read and
        `total_count` ignored, so a head with more runs than one page
        holds read as green whatever the runs past the page said. Now the
        pages are walked; a page the babysitter asked for and did not get
        leaves the read incomplete, which is pending."""
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
        """Review finding: a `required_status_checks` rule whose checks were
        not a list of contexts was silently dropped (green), and one whose
        `parameters` was not an object raised out of `pr_state`. Rules
        the babysitter cannot read are pending, like check runs it cannot
        read."""
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
        """Regression: the babysitter's fix commit is the implementer's work,
        and the pass after it -- green, quiet -- merged it with no
        independent look at that commit: both the review and the
        adjudication came before the fix. Now a candidate that moved
        since its approval is reviewed at the fixed sha before the merge
        API is called; the approving round is a `reviewRounds` row like
        the others."""
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
        """Regression: the review of the fix vouched for the merge gate
        too -- the fixed candidate went to the merge API on the review's
        verify alone, with no drift check, so a ticket edited while the
        fix round ran was merged against a contract that no longer
        existed. Now the fixed candidate goes through the gate: the run
        stops there, nothing is merged, and the ticket is told which
        fields moved."""
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
        """Regression: `--babysit` on a run parked because the review of
        the fix asked for changes resumed with the branch's HEAD taken as
        reviewed, so a green, quiet PR under `approve = "auto"` merged the
        rejected fix, unchanged, with no reviewer turn. The park now
        records the sha the last approval covered (none, here), and the
        resumed babysitter reviews the candidate again before any merge:
        another `REQUEST_CHANGES` parks it, unmerged, once more."""
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

    def park_on_a_declined_nit_with_a_bare_origin(self):
        """A run parked on a declined nit at its approved sha, and the
        target's `origin` then pointed at a bare repository holding the
        candidate branch -- the remote a person pushes on top of. Returns
        `(approved sha, bare path, scratch clone path)`; the clone is where
        a test makes the person's commits, and `publish()` moves them to
        the bare branch (`force` for a rewritten history)."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.NIT]), self.pr_state()])
        self.loop(Commit("the scripted work"), APPROVE,
                  Reply("THREAD 1: DECLINE -- a naming preference"),
                  provider=self.provider())
        approved = self.git("rev-parse", BRANCH).strip()
        for path in self.api_dir.iterdir():
            path.unlink()
        bare = self.worktrees.parent / "origin.git"
        self.git("init", "-q", "--bare", str(bare))
        self.git("fetch", "-q", str(self.target), f"{BRANCH}:{BRANCH}",
                 cwd=bare)
        self.git("remote", "set-url", "origin", str(bare))
        clone = self.worktrees.parent / "person"
        self.git("clone", "-q", "-b", BRANCH, str(bare), str(clone))
        self.git("config", "user.email", "person@example.invalid", cwd=clone)
        self.git("config", "user.name", "A Person", cwd=clone)
        holophyte.operator.babysit_ticket(self.tgt, "KO-131", "look again",
                                       out=io.StringIO())
        return approved, bare, clone

    def publish(self, clone, bare, force=False):
        """The person's branch in `clone` moved onto the bare remote --
        by a fetch into the bare repository, since the fixture's `git
        push` is the witnessed fake."""
        spec = f"{'+' if force else ''}{BRANCH}:{BRANCH}"
        self.git("fetch", "-q", str(clone), spec, cwd=bare)
        return self.git("rev-parse", BRANCH, cwd=bare).strip()

    def test_a_babysit_resume_fast_forwards_to_the_remote_branch(self):
        """A person pushed one commit on top of the parked candidate. The
        resume fetches the branch, fast-forwards the worktree and the
        local branch to the remote's head, notes the fast-forward in the
        ledger naming one commit, and judges that head against the
        approved sha as a fix round's commit is judged: a review, whose
        approval lets the green, quiet PR merge."""
        approved, bare, clone = self.park_on_a_declined_nit_with_a_bare_origin()
        (clone / "README.md").write_text("a person's touch\n")
        self.git("commit", "-q", "-am", "operator: adjust the candidate",
                 cwd=clone)
        theirs = self.publish(clone, bare)
        self.assertNotEqual(theirs, approved)

        fake, _ = self.loop(APPROVE, provider=self.provider())

        # The merged run removes the worktree and branch at close-out, so
        # the fast-forward is witnessed by what the review was handed and
        # the sha the merge recorded, not by a branch that no longer exists.
        self.assertEqual(fake.roles, ["review"])
        self.assertEqual(fake.turns[0].candidate_sha, theirs)
        # The new head is judged against the sha the reviewer approved,
        # as a fix round's commit is: the brief names that approval, and
        # would say the last review asked for changes had it been lost.
        self.assertIn(f"candidate was approved at {approved[:12]}",
                      fake.turns[0].goal)
        self.assertEqual(
            self.read("SELECT summary FROM runEvents WHERE runId = 2 AND"
                      " summary LIKE 'resuming run%'"),
            [(f"resuming run 1's candidate {BRANCH} at {theirs[:12]} on"
              f" {self.URL} for another babysit pass",)])
        self.assertEqual(
            self.read("SELECT text FROM ledger WHERE kind = 'note' AND text"
                      " LIKE 'Fast-forwarded%'"),
            [(f"Fast-forwarded {BRANCH} to {theirs} from origin (1 commit(s)"
              " pushed by someone else)",)])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge"])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"
                                   " WHERE id = 2"),
                         [("merged", self.MERGE_SHA)])

    def test_a_babysit_resume_parks_when_the_branches_diverged(self):
        """The remote branch was rewritten past the candidate rather than
        built on it. Nothing fast-forwards: the worktree and the local
        branch stay at the candidate, and the run parks with a question
        naming both shas, no review and no merge."""
        approved, bare, clone = self.park_on_a_declined_nit_with_a_bare_origin()
        self.git("reset", "-q", "--hard", "HEAD~1", cwd=clone)
        (clone / "README.md").write_text("rewritten\n")
        self.git("commit", "-q", "-am", "operator: a rewrite", cwd=clone)
        theirs = self.publish(clone, bare, force=True)

        fake, _ = self.loop(provider=self.provider())

        self.assertEqual(fake.roles, [])
        self.assertEqual(self.api_calls(), [])
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), approved)
        self.assertEqual(self.git("rev-parse", "HEAD",
                                  cwd=self.worktrees / "ko-131-add-a-thing")
                         .strip(), approved)
        question = self.question()
        self.assertIn(approved[:12], question)
        self.assertIn(theirs[:12], question)
        self.assertIn("diverged", question)
        self.assertEqual(self.read("SELECT phase, outcome, candidateSha FROM"
                                   " runs WHERE id = 2"),
                         [("awaiting_merge_approval", None, approved)])

    def test_a_babysit_resume_with_an_equal_remote_writes_no_note(self):
        """The remote holds exactly the candidate: no note is written, and
        the pass goes on as before -- the approved sha merges with no
        second review."""
        approved, bare, clone = self.park_on_a_declined_nit_with_a_bare_origin()

        fake, _ = self.loop(provider=self.provider())

        self.assertEqual(fake.roles, [])
        self.assertEqual(
            self.read("SELECT summary FROM runEvents WHERE runId = 2 AND"
                      " summary LIKE 'resuming run%'"),
            [(f"resuming run 1's candidate {BRANCH} at {approved[:12]} on"
              f" {self.URL} for another babysit pass",)])
        self.assertEqual(
            self.read("SELECT text FROM ledger WHERE text LIKE"
                      " 'Fast-forwarded%'"), [])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge"])
        self.assertEqual(self.read("SELECT outcome FROM runs WHERE id = 2"),
                         [("merged",)])

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
        """Regression: the fix round commits part of its fix and leaves the
        rest uncommitted. The verify ran over the working tree, so it
        passed on a fix the commit does not hold, and the branch was pushed
        and the thread resolved on a partial fix. Now the candidate must be
        clean -- HEAD, the branch and the tree on one commit -- before it is
        verified; otherwise the run fails with nothing pushed, nothing
        posted, and the edits left in place for a human."""
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
        """Regression: a thread's later comments were dropped, so a bot's
        finding that the operator had since turned into a question read to
        the adjudicator as the finding alone -- answerable, fixable,
        resolvable. The whole conversation reaches the adjudicator, and a
        `HUMAN` park quotes it."""
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
        """A thread with more comments than one page holds: the babysitter
        fetches the next page of that thread's comments (`after` its
        cursor) before the adjudicator judges it, so the latest word in
        the thread is in the brief."""
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
        """Acceptance: a thread the adjudicator marks `HUMAN`: no reply is
        posted on it, no fix round runs, the run parks, and the ticket's
        question quotes the thread."""
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
        """Acceptance: one thread a person (`User`) opened beside one a bot
        opened, and an adjudicator that would ADDRESS anything it is shown.
        The person's thread never reaches the adjudicator -- the brief
        names the bot's thread alone -- and is recorded `HUMAN`, "opened by
        a person"; nothing is posted on either; the run parks with the
        person's thread quoted, as a `HUMAN` verdict parks it."""
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
        """Acceptance (KO-337): `human_threads = "act"`, a thread a person
        opened asking for a concrete change, and an adjudicator answering
        ADDRESS for it. The thread reaches the adjudicator and the fix
        round, the fix is pushed, a reply naming the sha is posted on it,
        and `resolve_thread` is never called: the thread is the person's
        to close."""
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
        """Acceptance (KO-337): `human_threads = "act"`, a person's thread
        the adjudicator would DECLINE beside a bot's it would ADDRESS. The
        bot's thread is fixed, replied to and resolved; the person's gets no
        reply and no resolve -- the factory never declines a person -- and
        the run parks with the person's thread quoted."""
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
        """Review finding (KO-337): `human_threads = "act"` and two bots'
        threads, one the adjudicator marks HUMAN and one ADDRESS. Bot
        handling is unchanged by the setting: no fix round runs, nothing is
        posted or resolved, and the run parks with the HUMAN thread
        quoted -- as it does under the default."""
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
        """Acceptance: `pr_rounds = 2` and a thread that keeps reappearing:
        two passes each fix and answer it, the third pass does not happen,
        and the run parks naming the cap with the thread listed."""
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
        """Regression: a PR whose first page of threads is all resolved and
        whose open thread is on the second page is not quiet. The babysitter
        walks the pages (`after` the first's cursor) before deciding, finds
        the thread and parks on it -- no merge, under `approve = "auto"`."""
        self.configure('[merge]\nmode = "pr"\n')
        full_page = [self.NIT] * holophyte.pr.THREADS_PAGE
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
        """Regression: the PR's head is a commit this run did not push (a
        concurrent push to the branch). Its green checks are that commit's,
        not the candidate's, so the pass judges nothing and parks naming
        both shas -- no adjudicator, no merge, under `approve = "auto"`."""
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

    def test_an_approval_of_an_open_pull_request_merges_it_through_the_api(
            self):
        """`--approve KO-n` on a run parked with a PR open is the human's
        "merge": the resumed run babysits the PR once more and, green and
        quiet, merges it through the API -- no implementer, no reviewer,
        no push, no local merge, main untouched."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        self.loop(Commit("the scripted work"), APPROVE,
                  provider=self.provider())
        approved = self.git("rev-parse", BRANCH).strip()
        self.calls.unlink()
        for path in self.api_dir.iterdir():
            path.unlink()
        holophyte.operator.approve(self.tgt, "KO-131", "looks fine",
                               out=io.StringIO())

        fake, guard = self.loop(provider=self.provider())

        self.assertEqual(fake.roles, [])
        self.assertEqual(guard.spawned, [])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge"])
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [])
        self.assertEqual(self.subjects(), ["base"])  # local main untouched
        self.assertNotIn(BRANCH, self.branches())
        self.assertEqual(
            self.read("SELECT id, phase, outcome, resumePhase, prUrl,"
                      " candidateSha, mergeSha FROM runs ORDER BY id"),
            [(1, "failed", "abandoned", "merge_gate", self.URL, approved,
              None),
             (2, "done", "merged", None, None, None, self.MERGE_SHA)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])

    def test_a_babysitter_release_parks_again_rather_than_merging(self):
        """`--babysit KO-n` is "look again", not "merge": the resumed run
        babysits the PR and, green and quiet under `approve = "human"`,
        parks again on the same URL."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        self.loop(Commit("the scripted work"), APPROVE,
                  provider=self.provider())
        holophyte.operator.babysit_ticket(self.tgt, "KO-131", "bots are done",
                                       out=io.StringIO())

        fake, _ = self.loop(provider=self.provider())

        self.assertEqual(fake.roles, [])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "state"])
        self.assertEqual(
            self.read("SELECT id, phase, outcome, prUrl FROM runs"
                      " ORDER BY id"),
            [(1, "failed", "abandoned", self.URL),
             (2, "awaiting_merge_approval", None, self.URL)])
        self.assertEqual(
            self.read('SELECT "action" FROM interventions'), [("babysit",)])


    # What GitHub says about a parked pull request when the reconcile asks
    # (`pr.PULL_QUERY`'s node): merged by a coworker, closed unmerged, open.
    MERGED_PULL = {"state": "MERGED", "merged": True,
                   "mergeCommit": {"oid": MergeModeFixture.MERGE_SHA},
                   "mergedBy": {"login": "coworker"}}
    CLOSED_PULL = {"state": "CLOSED", "merged": False, "mergeCommit": None,
                   "mergedBy": None}
    OPEN_PULL = {"state": "OPEN", "merged": False, "mergeCommit": None,
                 "mergedBy": None}

    def fake_client(self, *answers, rate=None):
        """The reconcile's GitHub, faked: `holophyte.pr.graphql` answers
        each ask with the next of `answers` (the last one forever) and
        records the pull request and variables it was asked about. An
        answer that is an exception is raised instead: GitHub down.
        `rate` is the `rateLimit` node every answer carries, when one
        does. Only the pull-status read is faked here: the babysitter's own
        reads and writes still go to the scripted `gh`."""
        asked = []
        real = holophyte.pr.graphql

        def graphql(target, pull, query, variables):
            if "mergedBy" not in query:
                return real(target, pull, query, variables)
            asked.append((pull.url, query, variables))
            node = answers[min(len(asked), len(answers)) - 1]
            if isinstance(node, Exception):
                raise node
            data = {"repository": {"pullRequest": node}}
            if rate is not None:
                data["rateLimit"] = rate
            return data

        patcher = patch.object(holophyte.pr, "graphql", graphql)
        patcher.start()
        self.addCleanup(patcher.stop)
        return asked

    def parked_on_pr(self, extra=""):
        """A run parked on its pull request under `approve = "human"`, the
        state every reconcile test starts from; `extra` is further config
        text appended after the `[merge]` table."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n' + extra)
        self.fake_route()
        self.loop(Commit("the scripted work"), APPROVE,
                  provider=self.provider())
        self.assertEqual(self.read("SELECT phase, prUrl FROM runs"),
                         [("awaiting_merge_approval", self.URL)])

    def test_a_pull_request_merged_on_github_ships_its_parked_run(self):
        """KO-359: a person merged the pull request on GitHub instead of
        saying `--approve`. The next pass asks GitHub once, and the merge
        is the approval: the parked run ends `merged` with the pull
        request's merge commit as its `mergeSha`, the ticket is `merged`
        and the board saw Done, the ledger names who merged it, the local
        branch is gone and the findings window, where a target renders
        one, shows the run."""
        self.parked_on_pr('[report]\nfindings = "repo"\n')
        asked = self.fake_client(self.MERGED_PULL)
        provider = StubProvider()

        out = self.main_output(provider=provider)

        self.assertEqual([(url, v["number"]) for url, _, v in asked],
                         [(self.URL, 7)])
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha, prUrl FROM runs"),
            [("done", "merged", self.MERGE_SHA, self.URL)])
        self.assertEqual(
            self.read("SELECT status, blockedQuestion FROM tickets"),
            [("merged", None)])
        self.assertEqual(provider.states, [("iss-131", "Done")])
        self.assertEqual(self.read('SELECT "action" FROM interventions'),
                         [("approve",)])
        (merge_line,) = [text for (text,) in self.read(
            "SELECT text FROM ledger WHERE kind = 'merge'")]
        self.assertIn("coworker", merge_line)
        self.assertIn(self.MERGE_SHA, merge_line)
        self.assertIn(f"{self.URL} was merged on GitHub by coworker", out)
        self.assertNotIn(BRANCH, self.branches())
        self.assertIn("KO-131", (self.target / "FINDINGS.md").read_text())
        self.assertEqual(self.subjects(), ["base"])  # local main not moved

    def test_a_merged_pull_request_ships_when_linear_already_says_done(self):
        """Review of KO-359: the person who merged the pull request on
        GitHub also moved the ticket to Done on the board. At startup the
        mirror reconcile used to see Done first and walk the ticket
        `merged` on its own, and the pull request was never asked about:
        the run stayed parked with no outcome and no `mergeSha`. GitHub
        is asked before the mirror is repaired, so the run ships."""
        self.parked_on_pr()
        asked = self.fake_client(self.MERGED_PULL)
        provider = StubProvider()
        provider.closed = {"KO-131": "completed"}

        self.main_output(provider=provider)

        self.assertEqual(len(asked), 1)
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("done", "merged", self.MERGE_SHA)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])
        self.assertEqual(self.read('SELECT "action" FROM interventions'),
                         [("approve",)])

    def test_a_github_error_leaves_the_parked_run_for_the_next_pass(self):
        """Review of KO-359: GitHub could not be read at startup while the
        board already said Done. The mirror reconcile used to take that
        Done and walk the ticket `merged` around its parked run, and no
        later pass asked GitHub about a ticket no longer blocked: the run
        was stranded with no outcome and no `mergeSha`. Now the ticket
        stays parked with its run through the failure, and the pass after
        GitHub recovers ships it."""
        self.parked_on_pr()
        asked = self.fake_client(RuntimeError("GitHub is down"),
                                 self.MERGED_PULL)
        provider = StubProvider()
        provider.closed = {"KO-131": "completed"}

        out = self.main_output(provider=provider)

        self.assertEqual(len(asked), 1)
        self.assertIn("could not be read (GitHub is down); the run stays"
                      " parked", out)
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("awaiting_merge_approval", None, None)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("blocked_on_operator",)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"),
                         [(0,)])

        again = StubProvider()
        again.closed = {"KO-131": "completed"}
        self.main_output(provider=again)

        self.assertEqual(len(asked), 2)
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("done", "merged", self.MERGE_SHA)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])
        self.assertEqual(again.states, [("iss-131", "Done")])
        self.assertEqual(self.read('SELECT "action" FROM interventions'),
                         [("approve",)])

    def test_a_pull_request_closed_without_merge_keeps_the_run_parked(self):
        """The pull request was closed on GitHub unmerged: the run stays
        parked, the ticket's question says so, the skip line reads the
        question rather than an `--approve` that would merge nothing, and
        a second pass finding the same neither writes nor prints again."""
        self.parked_on_pr()
        self.fake_client(self.CLOSED_PULL)

        out = self.main_output(provider=StubProvider(
            dict(a_task(), body=self.BODY)))
        again = self.main_output(provider=StubProvider())

        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl FROM runs"),
            [("awaiting_merge_approval", None, self.URL)])
        self.assertEqual(self.question(),
                         f"PR closed without merge: {self.URL}")
        self.assertIn(f"[holo2] KO-131 is parked on a question: PR closed"
                      f" without merge: {self.URL}; skipping it\n", out)
        self.assertNotIn("--approve", out)
        self.assertIn("closed on GitHub without merging", out)
        self.assertNotIn("closed on GitHub", again)
        self.assertEqual(self.last_provider.states, [])

    def test_an_open_pull_request_is_asked_about_once_and_left_alone(self):
        self.parked_on_pr()
        runs = self.read("SELECT * FROM runs")
        tickets = self.read("SELECT * FROM tickets")
        asked = self.fake_client(self.OPEN_PULL)
        provider = StubProvider()

        self.main_output(provider=provider)

        self.assertEqual(len(asked), 1)
        self.assertEqual(self.read("SELECT * FROM runs"), runs)
        self.assertEqual(self.read("SELECT * FROM tickets"), tickets)
        self.assertEqual(provider.states, [])
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"),
                         [(0,)])

    # The open pull request as it reads with review activity on it (KO-362):
    # `updatedAt` and the thread count are what the reconcile holds against
    # the run's mark.
    T1, T2, T3 = ("2026-09-10T10:00:00Z", "2026-09-10T11:00:00Z",
                  "2026-09-10T11:00:30Z")

    def open_pull(self, at, threads, checks=None, review=None):
        """The open pull request read at `at` with `threads` review
        threads; `checks` is the head's `statusCheckRollup.state` and
        `review` GitHub's `reviewDecision`, both absent when None (a PR
        with no checks, a repository requiring no review)."""
        pull = dict(self.OPEN_PULL, updatedAt=at,
                    reviewThreads={"totalCount": threads})
        if checks is not None:
            pull["commits"] = {"nodes": [
                {"commit": {"statusCheckRollup": {"state": checks}}}]}
        if review is not None:
            pull["reviewDecision"] = review
        return pull

    def parked_with_mark(self, at, threads):
        """A run parked on its pull request whose park recorded `at` and
        `threads` as what it saw, parked long enough ago for
        `[merge] pr_poll_sec` to have passed."""
        self.parked_on_pr()
        self.assertEqual(self.read("SELECT prSeenAt, prSeenThreads FROM"
                                   " runs"), [(None, None)])
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE runs SET prSeenAt = ?, prSeenThreads = ?,"
                         " lastHeartbeat = lastHeartbeat - 200000",
                         (at, threads))
        conn.close()

    def test_new_review_activity_sends_the_parked_run_to_the_babysit(
            self):
        """KO-362: the pull request's `updatedAt` moved past what the last
        pass recorded. The tick sends the run back to the babysitter as
        `--babysit` would -- a `babysit` intervention, the run ended
        with the ticket ready -- and the same pass claims it: the resumed
        run babysits the pull request and parks again, its park recording
        what it saw *after* its own writes (the third answer), so the tick
        after that, reading the same, sends nothing."""
        self.parked_with_mark(self.T1, 0)
        asked = self.fake_client(
            self.open_pull(self.T2, 1, checks="PENDING",
                           review="CHANGES_REQUESTED"),
            self.open_pull(self.T3, 1, checks="SUCCESS", review="APPROVED"))

        out = self.main_output(provider=self.provider())

        self.assertIn(f"KO-131: {self.URL} has new review activity (updated"
                      f" {self.T2}, 1 review threads); run 1 sent back to"
                      " the babysitter", out)
        # Each write records the checks rollup and review decision the
        # same read saw beside the mark (KO-368).
        self.assertEqual(
            self.read("SELECT id, phase, outcome, prSeenAt, prSeenThreads,"
                      " prSeenChecks, prSeenReview FROM runs ORDER BY id"),
            [(1, "failed", "abandoned", self.T2, 1, "pending",
              "changes_requested"),
             (2, "awaiting_merge_approval", None, self.T3, 1, "success",
              "approved")])
        self.assertEqual(
            self.read('SELECT "action", source FROM interventions'),
            [("babysit", "supervisor")])
        self.assertEqual(
            self.read("SELECT summary FROM runEvents"
                      " WHERE kind = 'intervention'"),
            [(f"supervisor babysit: new review activity on {self.URL}:"
              f" updated {self.T2} (last seen {self.T1}), 1 review threads"
              " (last seen 0)",)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("blocked_on_operator",)])
        # Three reads so far: the tick's, the park's after its writes,
        # and the pass after the park's, which saw the same and sent
        # nothing.
        self.assertEqual(len(asked), 3)

        again = self.main_output(provider=StubProvider())

        self.assertEqual(len(asked), 4)
        self.assertNotIn("new review activity", again)
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"),
                         [(1,)])
        self.assertEqual(self.read("SELECT phase, prSeenAt FROM runs"
                                   " WHERE id = 2"),
                         [("awaiting_merge_approval", self.T3)])

    def test_an_unchanged_pull_request_is_not_babysat_again(self):
        """Two ticks over the same pull request: the first finds no mark
        on the run (parked by a module older than the columns) and records
        what it saw without babysitting; the second finds the same and
        does nothing. No round, no intervention, the run still parked."""
        self.parked_on_pr()
        asked = self.fake_client(self.open_pull(self.T1, 0, checks="SUCCESS"))

        first = self.main_output(provider=StubProvider())
        second = self.main_output(provider=StubProvider())

        self.assertEqual(len(asked), 2)
        self.assertNotIn("review activity", first + second)
        # No `reviewDecision` in the answer is a null review, not an error.
        self.assertEqual(
            self.read("SELECT phase, outcome, prSeenAt, prSeenThreads,"
                      " prSeenChecks, prSeenReview FROM runs"),
            [("awaiting_merge_approval", None, self.T1, 0, "success", None)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"),
                         [(0,)])

    def test_an_unchanged_pull_request_still_refreshes_its_facts(self):
        """KO-368 review round 1: the run holds a mark from the last pass
        and facts from the same read (checks pending, review required).
        The pull request has not moved -- same `updatedAt`, same thread
        count -- but its checks went green and a review landed. The tick
        starts no round and leaves the mark alone, yet the facts on the
        run are what the read saw, not what the last pass saw."""
        self.parked_with_mark(self.T1, 0)
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE runs SET prSeenChecks = 'pending',"
                         " prSeenReview = 'review_required'")
        conn.close()
        self.fake_client(self.open_pull(self.T1, 0, checks="SUCCESS",
                                        review="APPROVED"))

        out = self.main_output(provider=StubProvider())

        self.assertNotIn("review activity", out)
        self.assertEqual(
            self.read("SELECT phase, prSeenAt, prSeenThreads, prSeenChecks,"
                      " prSeenReview FROM runs"),
            [("awaiting_merge_approval", self.T1, 0, "success",
              "approved")])
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"),
                         [(0,)])

    def test_activity_within_the_poll_interval_waits(self):
        """The pull request moved, but the run parked seconds ago: the tick
        names the activity and the wait rather than starting a round."""
        self.parked_on_pr()
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE runs SET prSeenAt = ?, prSeenThreads = 0",
                         (self.T1,))
        conn.close()
        self.fake_client(self.open_pull(self.T2, 1))

        out = self.main_output(provider=StubProvider())

        self.assertIn(f"KO-131: {self.URL} has new review activity; the next"
                      " babysit round waits", out)
        self.assertIn("([merge] pr_poll_sec)", out)
        self.assertEqual(self.read("SELECT phase, prSeenAt FROM runs"),
                         [("awaiting_merge_approval", self.T1)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"),
                         [(0,)])

    def test_a_low_github_budget_stops_the_pull_request_reads(self):
        """The read that finds `rateLimit.remaining` under the floor is the
        last one until the reset it names: the activity it saw starts no
        round, the tick prints one line naming `resetAt`, and the next tick
        reads no pull request at all."""
        self.parked_with_mark(self.T1, 0)
        reset = "2999-01-01T00:00:00Z"
        asked = self.fake_client(self.open_pull(self.T2, 1),
                                 rate={"remaining": 200, "resetAt": reset})

        first = self.main_output(provider=self.provider())
        second = self.main_output(provider=self.provider())

        self.assertEqual(len(asked), 1)
        line = ("[holo2] GitHub's GraphQL budget is down to 200 points; no"
                f" parked pull request is read until it resets at {reset}")
        self.assertIn(line, first)
        self.assertIn(line, second)
        self.assertNotIn("sent back to the babysitter", first + second)
        self.assertEqual(self.read("SELECT phase, prSeenAt FROM runs"),
                         [("awaiting_merge_approval", self.T1)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"),
                         [(0,)])

    def test_the_scheduler_ships_a_merged_pull_request_on_a_timer_tick(
            self):
        """The pool's timer tick asks too: open at startup, merged by the
        tick, and the parked run is closed out between two waits with no
        worker involved (KO-353's tick carrying KO-359's reconcile)."""
        self.parked_on_pr()
        asked = self.fake_client(self.OPEN_PULL, self.MERGED_PULL)
        provider = StubProvider(a_task(2))
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       '[loop]\nworkers = 2\ntick_sec = 30\n')
        pool = FakePool([(TICK, provider.queue.clear),
                         (holophyte.pool.WORKER_MERGED, None)])
        out = io.StringIO()
        with patch.object(holophyte.pool, "SPAWN", pool.spawn), \
                patch.object(holophyte.pool, "WAIT", pool.wait), \
                patch.object(sys, "stdout", out):
            rc = holophyte.operator.main(self.tgt, provider)

        self.assertIsNone(rc)
        self.assertEqual(len(asked), 2)
        self.assertEqual(pool.timeouts, [30, 30])
        self.assertEqual(len(pool.spawned), 1)
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("done", "merged", self.MERGE_SHA)])
        self.assertEqual(
            self.read("SELECT status FROM tickets"
                      " WHERE linearIdentifier = 'KO-131'"),
            [("merged",)])
        self.assertIn(("iss-131", "Done"), provider.states)
        self.assertIn(f"{self.URL} was merged on GitHub", out.getvalue())

    def test_a_refused_push_is_an_infra_failure_with_no_pull_request(self):
        """The remote said no: the run ends as an infra failure naming the
        push, the branch and worktree are preserved, `gh` was never called
        and nothing is recorded as a PR."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(push_exit=1)

        self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(self.recorded(), [f"git push origin {BRANCH}"])
        self.assertFalse(self.pr_body.exists())
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertIn(BRANCH, self.branches())
        self.assertTrue((self.worktrees / "ko-131-add-a-thing").exists())
        rows = self.read("SELECT phase, outcome, outcomeClass, outcomeReason,"
                         " prUrl FROM runs")
        (phase, outcome, klass, reason, url), = rows
        self.assertEqual((phase, outcome, klass, url),
                         ("failed", "failed", "infra", None))
        self.assertIn(f"git push origin {BRANCH} failed", reason)
        self.assertIn("refused", reason)

    def test_local_merges_as_today_and_pushes_nothing(self):
        self.configure('[merge]\nmode = "local"\n')
        self.fake_route()

        self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(self.recorded(), [])
        self.assertIn("the scripted work", self.subjects())
        self.assertNotIn(BRANCH, self.branches())
        self.assertEqual(self.read("SELECT outcome, prUrl FROM runs"),
                         [("merged", None)])


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
            ok, merged = holophyte.loop._merge_gate(
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
                holophyte.loop._sync_main_into_branch(
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
                holophyte.loop._sync_main_into_branch(
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
                holophyte.loop._sync_main_into_branch(
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
                holophyte.loop._sync_main_into_branch(
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
            merged = holophyte.loop._sync_main_into_branch(
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
