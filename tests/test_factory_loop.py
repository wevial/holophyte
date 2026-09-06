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
    REVIEW_ROLES,
    Commit,
    FakeAgent,
    Idle,
    Reply,
    no_agent_processes,
)

import holophyte.agents  # noqa: E402 - after the sys.path insert above
import holophyte.board  # noqa: E402 - after the sys.path insert above
import holophyte.config  # noqa: E402 - after the sys.path insert above
import holophyte.gates  # noqa: E402 - after the sys.path insert above
import holophyte.loop  # noqa: E402 - after the sys.path insert above
import holophyte.pr  # noqa: E402 - after the sys.path insert above
import holophyte.supervisor  # noqa: E402 - after the sys.path insert above
import holophyte.target  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above

# The branch the loop cuts for the task below. Spelled out rather than derived
# from `factory`'s slug rule: an expectation computed by the code under test
# is not an expectation.
BRANCH = "task/ko-131-add-a-thing"


class StubProvider:
    """The provider seam `main()` drives, queueing tasks it hands out in order."""

    TEAM = "team-under-test"
    team = TEAM  # the `Provider` protocol's spelling

    def __init__(self, *tasks):
        self.queue = list(tasks)
        # What `fetch_task()` hands back, kept apart from the queue so a test
        # can leave the live ticket saying something other than the one that
        # was claimed — the mid-run edit the merge gate exists to catch. The
        # loop only reads it at the gate, so seeding it up front and editing
        # it during the run are the same thing from the loop's side.
        self.live = {task["issue_id"]: task for task in tasks}
        self.states = []
        self.comments = []

    def claim_next(self, skip=(), order="identifier"):
        """The first queued task the loop has not already refused.

        `skip` is honored rather than ignored because the real provider hands
        back the *same* head-of-queue ticket on every ask; a stub that popped
        blindly would let a loop that cannot skip look like one that can.
        """
        for i, task in enumerate(self.queue):
            if task["id"] not in skip:
                return self.queue.pop(i)
        return None

    def fetch_task(self, issue_id):
        """The ticket as the board holds it now; None when there is no such issue."""
        task = self.live.get(issue_id)
        return dict(task) if task else None

    def set_state(self, issue_id, state):
        self.states.append((issue_id, state))

    def comment(self, task_id, body):
        self.comments.append((task_id, body))


def a_task(n=1):
    """One ticket in the shape `linear_provider.parse_task()` returns."""
    return {"id": f"KO-13{n}", "issue_id": f"iss-13{n}", "title": "add a thing",
            "verify": "echo ok", "budget_min": 5, "contracts": [],
            "criteria": ["Given the thing, when it runs, then it works"]}


class LoopFixture(unittest.TestCase):
    """The real repo, worktree directory and store every loop test runs on.

    Split from the tests so a suite with its own configuration — the
    `[worktree]` one below — reuses the fixture without re-running the tests
    that came with it.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.target = root / "repo"
        self.worktrees = root / "repo.worktrees"
        self.target.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "factory@example.invalid")
        self.git("config", "user.name", "Factory Test")
        (self.target / "README.md").write_text("base\n")
        # The test the scripted approvals name as their witness: since KO-215
        # the loop checks a named test exists in the worktree.
        (self.target / "tests").mkdir()
        (self.target / "tests" / "test_thing.py").write_text(
            "def test_it_works():\n    pass\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "main").strip()

        # Where `Target.locate(self.target)` will look: the target's directory
        # under a HOLOPHYTE_HOME of this test's own, never the operator's real
        # one.
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(root / "home")})
        home.start()
        self.addCleanup(home.stop)
        self.db = holophyte.target.state_dir(self.target) / "store.db"
        self.db.parent.mkdir(parents=True)
        self.tgt = holophyte.target.Target.locate(self.target)
        assert self.tgt.store_path == self.db
        assert self.tgt.worktrees == self.worktrees

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.target),
                              check=True, capture_output=True, text=True).stdout

    def configure(self, toml):
        """Give the fixture target a config file and a `Target` that reads it.

        Through `Target.locate()` rather than by writing `config_path` by
        hand: it derives every path from the target the same way the fixture
        does, so a test that set the config by hand could pass with the file
        unwired. A fresh value, too: a `Target` parses its config once.
        """
        (self.db.parent / "config.toml").write_text(toml)
        self.tgt = holophyte.target.Target.locate(self.target)

    def loop(self, *script, provider=None):
        """Run `main()` over the queued tasks with the script answering agents.

        Returns the fake and the spawn guard, so a test can read both the
        turns the loop took and the processes it did not start; `main()`'s
        return code lands in `self.rc` for the tests that pin the exit
        contract.
        """
        fake = FakeAgent(*script)
        provider = provider or StubProvider(a_task())
        self.last_provider = provider
        self.last_fake = fake
        with no_agent_processes() as guard:
            with patch.dict(sys.modules, {"linear_provider": provider}):
                with patch.object(holophyte.loop, "agent", fake):
                    self.rc = holophyte.loop.main(self.tgt, provider)
        return fake, guard

    def main_output(self, *script, provider=None):
        out = io.StringIO()
        with patch.object(sys, "stdout", out):
            self.loop(*script, provider=provider)
        return out.getvalue()

    def read(self, sql):
        """Query the store over a connection the factory never touched."""
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn.execute(sql).fetchall()

    def subjects(self, rev="main"):
        return self.git("log", rev, "--format=%s").splitlines()

    def transitions(self):
        """The edges the run's narrative stream says it walked."""
        return [summary.split(":")[0] for (summary,) in
                self.read("SELECT summary FROM runEvents ORDER BY seq")]

    def branches(self):
        return [line[2:].strip() for line in
                self.git("branch", "--list").splitlines()]


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
        self.assertEqual(
            self.read("SELECT runId, kind FROM ledger ORDER BY at, id"),
            [(run_id, "round"), (run_id, "merge")])
        self.assertEqual([len(seen) for seen in provider.seen], [1, 2])
        self.assertEqual([seen[-1][0] for seen in provider.seen],
                         ["round", "merge"])
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
        self.assertEqual(entries, [(run_id, "round", "loop"),
                                   (run_id, "round", "loop"),
                                   (run_id, "merge", "loop")])
        # Three comments, and at each one the table already held the entry
        # the comment carries: one row at the findings round's comment, two
        # at the approving round's, three at the merge's.
        self.assertEqual(len(provider.comments), 3)
        self.assertEqual([len(seen) for seen in provider.seen], [1, 2, 3])
        for (_task, body), seen in zip(provider.comments, provider.seen):
            kind, text = seen[-1]
            self.assertIn(text, body)
        self.assertEqual([seen[-1][0] for seen in provider.seen],
                         ["round", "round", "merge"])

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
        store.walk_ticket(conn, 1, "ready")

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
        store.walk_ticket(conn, 1, "ready")

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

        self.loop(Commit("the other work"), APPROVE,
                  provider=StubProvider(blocked, other))

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


class WorktreeSetupLoopTests(LoopFixture):
    """`[worktree] setup` as a whole run walks it: real repo, real worktree.

    The unit tests cover the table and the report. What only a run can show is
    where the commands land in the loop — after the branch is cut, before the
    first agent turn — and what a failing setup does to the run around it.
    """

    def test_setup_runs_in_the_fresh_worktree_before_the_implementer(self):
        """The commands run in the task worktree — not the main checkout —
        while the branch is cut and before any agent turn, and the run merges
        as it otherwise would."""
        marker = self.target.parent / "where.txt"
        self.configure(f'[worktree]\nsetup = ["pwd > {marker}"]\n')

        fake, guard = self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(marker.read_text().strip(),
                         str((self.worktrees / "ko-131-add-a-thing").resolve()))
        self.assertEqual(guard.spawned, [])
        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertIn("the scripted work", self.subjects())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_failed_setup_fails_the_run_before_any_agent_turn(self):
        """No agent is dispatched — the script is empty, so a turn would raise
        — main is untouched, and the branch is discarded rather than preserved:
        nothing was implemented on it."""
        provider = StubProvider(a_task(1), a_task(2))
        self.configure('[worktree]\nsetup = ["echo no toolchain here; exit 3"]\n')

        fake, guard = self.loop(provider=provider)

        self.assertEqual(fake.roles, [])
        self.assertEqual(guard.spawned, [])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertNotIn(BRANCH, self.branches())
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        # A toolchain outage says nothing about the ticket: no agent ran, so
        # the failure must not spend one of its escalation strikes.
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(len(provider.queue), 1)  # the loop stopped
        # The ticket carries the reason, with the failing command and what it
        # printed — a run that ended before anything ran leaves no other trace.
        (_, body), = provider.comments
        self.assertIn("worktree setup", body)
        self.assertIn("no toolchain here", body)
        self.assertIn("exit 3", body)

    def test_a_failing_setup_leaves_a_reused_worktree_as_found(self):
        """A setup failure says nothing about the preserved work a reused
        worktree may hold; only a branch the run cut fresh is discarded."""
        wt = self.worktrees / "ko-131-add-a-thing"
        self.git("worktree", "add", "--detach", str(wt), "main")
        self.git("checkout", "-b", BRANCH, cwd=wt)
        (wt / "rescued.txt").write_text("rescued work\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", "rescued: preserved work", cwd=wt)
        self.configure('[worktree]\nsetup = ["exit 3"]\n')

        self.loop()

        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertTrue((wt / "rescued.txt").exists())
        self.assertIn("rescued: preserved work", self.subjects(BRANCH))
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("left in place", reason)

    def test_the_setup_phase_is_recorded_between_cutting_and_working(self):
        self.configure('[worktree]\nsetup = ["true"]\n')

        self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(self.transitions()[:3],
                         ["claimed -> working", "working -> working",
                          "working -> verifying"])
        (note,) = [summary for (summary,) in
                   self.read("SELECT summary FROM runEvents ORDER BY seq")
                   if "worktree setup" in summary]
        self.assertIn("1 command(s)", note)


class TicketNameTests(LoopFixture):
    """The branch and worktree a run cuts are named after the ticket, not the
    title alone: the identifier leads, so an operator can map any preserved
    `task/*` branch back to its ticket from `git branch`, and two titles that
    truncate to the same slug never land in the same worktree."""

    def merges(self):
        return [s for s in self.subjects() if s.startswith("Merge task/")]

    def test_the_branch_and_worktree_carry_the_lowercased_identifier(self):
        provider = StubProvider({**a_task(), "id": "KO-150",
                                 "title": "Supervisor 5/5: config"})

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            provider=provider)

        self.assertEqual(fake.turns[0].cwd.name, "ko-150-supervisor-5-5-config")
        self.assertEqual(self.merges(),
                         ["Merge task/ko-150-supervisor-5-5-config: "
                          "Supervisor 5/5: config"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_titles_sharing_a_thirty_character_prefix_get_distinct_names(self):
        """Both titles truncate to `supervisor-worktree-reuse-on-a`; without
        the identifier the second run would land in the first's worktree."""
        shared = "Supervisor: worktree reuse on "
        self.assertEqual(len(shared), 30)
        provider = StubProvider(
            {**a_task(1), "title": shared + "a clean failure"},
            {**a_task(2), "title": shared + "a dirty failure"})

        fake, _ = self.loop(Commit("first ticket"), APPROVE,
                            Commit("second ticket"), APPROVE,
                            provider=provider)

        cut = [turn.cwd.name for turn in fake.turns if turn.role == "implement"]
        self.assertEqual(len(cut), 2)
        self.assertNotEqual(cut[0], cut[1])
        merged = self.merges()
        self.assertEqual(len(merged), 2)
        self.assertNotEqual(merged[0].split(":")[0], merged[1].split(":")[0])
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("merged",), ("merged",)])

    def test_the_prefix_comes_from_the_worktree_table(self):
        """`[worktree] branch_prefix = "factory"` puts `factory/` ahead of the
        identifier; the worktree directory does not carry it and is unchanged."""
        self.configure('[worktree]\nbranch_prefix = "factory"\n')
        provider = StubProvider({**a_task(), "id": "KO-7000"})

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            provider=provider)

        self.assertEqual(fake.turns[0].cwd.name, "ko-7000-add-a-thing")
        self.assertEqual(
            [s for s in self.subjects() if s.startswith("Merge ")],
            ["Merge factory/ko-7000-add-a-thing: add a thing"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_worktree_table_without_the_key_keeps_the_task_prefix(self):
        self.configure('[worktree]\nsetup = ["true"]\n')
        provider = StubProvider({**a_task(), "id": "KO-7000"})

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            provider=provider)

        self.assertEqual(fake.turns[0].cwd.name, "ko-7000-add-a-thing")
        self.assertEqual(self.merges(),
                         ["Merge task/ko-7000-add-a-thing: add a thing"])


class LeftoverWorktreeTests(LoopFixture):
    def leftover(self):
        """A registered leftover worktree on BRANCH, as a failed run leaves it."""
        wt = self.worktrees / "ko-131-add-a-thing"
        self.git("worktree", "add", "--detach", str(wt), "main")
        self.git("checkout", "-b", BRANCH, cwd=wt)
        return wt

    def test_an_idle_implementer_on_a_dirty_leftover_does_not_merge_debris(self):
        """The WIP commit reuse makes is a candidate for review, not a free
        pass to main: an implementer that does nothing on a reused worktree
        sends the carried tip to the reviewer, and only an approval there can
        put the leftover's debris on main."""
        wt = self.leftover()
        (wt / "debris.bin").write_text("build junk\n")

        fake, _ = self.loop(Idle(), REQUEST_CHANGES, Idle())

        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        # The debris survives as the WIP commit, on a branch nothing merged.
        self.assertIn(BRANCH, self.branches())
        self.assertIn("WIP", self.subjects(BRANCH)[0])

    def test_an_empty_reused_leftover_is_discarded_like_a_fresh_cut(self):
        """A clean leftover at main holds nothing: keeping it forever and
        calling it preserved work would be the reason lying in the safe
        direction — and an unbounded leftover on every re-failing ticket."""
        self.leftover()

        self.loop(Idle())

        self.assertNotIn(BRANCH, self.branches())
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("discarded", reason)

    def test_a_timed_out_implementer_keeps_the_commits_it_made(self):
        """A budget overrun is not 'no work': commits that landed before the
        alarm survive, with the reason saying where they are."""
        printed = self.main_output(CommitThenTimeout("late work"))

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertIn(BRANCH, self.branches())
        self.assertIn("late work", self.subjects(BRANCH))
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("budget", reason)
        self.assertIn(BRANCH, reason)
        # The cap, not an alarm: the budget reaches the dispatch as its
        # timeout, and what the turn printed before the kill is not lost.
        self.assertEqual(self.last_fake.turns[0].timeout, 5 * 60)
        self.assertIn("partial progress before cap", printed)

    def test_the_refusal_reason_reaches_the_run_row(self):
        """The reuse refusal's whole product is an explanation for a human;
        it must land on the run row, not only in a Linear comment a provider
        outage can swallow."""
        wt = self.worktrees / "ko-131-add-a-thing"
        wt.mkdir(parents=True)
        (wt / "precious.txt").write_text("rescued work\n")

        self.loop()

        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("not a registered worktree", reason)
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])

    def test_a_no_commit_run_keeps_the_reused_worktree_and_its_commits(self):
        """Run 10 of the KO-146 incident: the no-commit close-out
        force-removed the reused worktree and -D'd the branch, destroying
        exactly the preserved work the reuse path exists to protect — and
        the run row then claimed the branch was preserved.

        The branch here is ahead of main in history but identical to it in
        content, so there is no carried candidate to review (KO-172) and the
        no-commit gate is still what closes the run out."""
        wt = self.leftover()
        (wt / "rescued.txt").write_text("rescued work\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", "rescued: preserved work", cwd=wt)
        self.git("rm", "-q", "rescued.txt", cwd=wt)
        self.git("commit", "-q", "-m", "rescued: and taken back out", cwd=wt)

        fake, _ = self.loop(Idle())

        self.assertEqual(fake.roles, ["implement"])  # no review turn
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertIn(BRANCH, self.branches())
        self.assertIn("rescued: preserved work", self.subjects(BRANCH))
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("preserved work kept on", reason)

    def test_a_carried_candidate_reaches_review_without_a_new_commit(self):
        """A preserved branch ahead of main is a candidate, not a dead run:
        the implementer that correctly no-ops on finished work used to fail
        the no-commit gate forever, so the only exits were operator surgery
        or destroying the work (holophyte-bugs #3)."""
        wt = self.leftover()
        (wt / "carried.txt").write_text("a complete candidate\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", "carried: a complete candidate", cwd=wt)
        carried = self.git("rev-parse", "HEAD", cwd=wt).strip()

        fake, _ = self.loop(Idle(), APPROVE)

        self.assertEqual(fake.roles, ["implement", "review"])
        review = next(t for t in fake.turns if t.role == "review")
        self.assertEqual((review.base_sha, review.candidate_sha),
                         (self.base, carried))
        self.assertIn("carried: a complete candidate", self.subjects())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertTrue(
            [summary for (summary,) in
             self.read("SELECT summary FROM runEvents ORDER BY seq")
             if "candidate carried from a prior run" in summary],
            "no event names the candidate as carried")

    def test_a_fresh_no_commit_run_cleans_up_and_says_discarded(self):
        """The fresh-cut behavior stays: nothing on the branch to keep, so
        it goes — and the reason says so instead of claiming preservation."""
        self.loop(Idle())

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertNotIn(BRANCH, self.branches())
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("discarded", reason)
        self.assertNotIn("preserved", reason)

    def test_preserved_commits_are_reviewed_and_carried_into_the_merge(self):
        """Preserved commits were never approved, so the review base must be
        main — putting them inside the reviewed diff — and the merge must
        carry them into main's history."""
        wt = self.leftover()
        (wt / "rescued.txt").write_text("rescued work\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", "rescued: preserved work", cwd=wt)

        fake, _ = self.loop(Commit("the scripted work"), APPROVE)

        self.assertIn("rescued: preserved work", self.subjects())
        review = next(t for t in fake.turns if t.role == "review")
        self.assertEqual(review.base_sha, self.base)

    def test_an_unregistered_leftover_directory_fails_the_run_cleanly(self):
        """A leftover directory that is not a registered worktree can be
        neither reused nor safely deleted, so the run fails with nothing
        under the directory touched — before the fix `git worktree add`
        died on the non-empty directory and the RuntimeError escaped
        `main()` as a traceback (KO-146 incident, run 9's sibling)."""
        wt = self.worktrees / "ko-131-add-a-thing"
        wt.mkdir(parents=True)
        (wt / "precious.txt").write_text("rescued work\n")

        provider = StubProvider(a_task())
        self.loop(provider=provider)  # no agent turns: fails before dispatch

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual((wt / "precious.txt").read_text(), "rescued work\n")
        (body,) = [body for _task_id, body in provider.comments
                   if "not a registered worktree" in body]
        self.assertIn(str(wt), body)

    def test_a_leftover_branch_with_no_directory_fails_the_run_cleanly(self):
        """The mirror leftover: a preserved branch whose directory a human
        cleared away. `checkout -b` dies on the existing branch, so before
        the fix the RuntimeError escaped `main()`; deleting the branch
        instead could destroy preserved commits."""
        self.git("branch", BRANCH, "main")

        self.loop()  # no agent turns: the run fails before dispatch

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertIn(BRANCH, self.branches())


class SweepDiagnosticsTests(LoopFixture):
    """A refused claim and the startup preamble surface the read-only sweep.

    The KO-146 incident's dead end: "lease already held by run 7" with
    nothing about whether run 7 was alive, and no strike recorded, so the
    relaunch reflex never accumulated evidence. One read-only sweep per
    invocation turns the relaunch into the evidence — the second launch can
    act.
    """

    MINUTE = 60 * 1000

    def stale_holder(self, minutes_silent=6, strikes=0):
        """A run some other loop claimed and went silent on, lease held."""
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        store.init(conn)
        project = store.ensure_project(conn, StubProvider.TEAM,
                                       str(self.target))
        ticket = store.mirror_ticket(
            conn, project, linear_issue_id="iss-stale",
            linear_identifier="KO-9", title="stalled elsewhere",
            acceptance_criteria=["Given a run, then it heartbeats"],
            verification_commands=["echo ok"], time_box_ms=25 * self.MINUTE)
        store.transition(conn, ticket, "in_flight")
        then = int(time.time() * 1000) - minutes_silent * self.MINUTE
        run_id = store.claim(conn, project, ticket, now=then)
        store.set_phase(conn, run_id, "working", now=then)
        if strikes:
            store.record_strike(conn, run_id, True, then, now=then + 1)
        return run_id

    def test_a_refused_claim_prints_the_silence_and_records_a_strike(self):
        run_id = self.stale_holder()

        printed = self.main_output()

        self.assertIn("claim refused", printed)
        self.assertIn(f"run {run_id}", printed)
        # One sweep, printed once: the refusal points back at it rather than
        # re-sweeping (double-counting the silence) or reprinting.
        self.assertEqual(printed.count("strike 1 of 2"), 1)
        self.assertLess(printed.index("strike 1 of 2"),
                        printed.index("claim refused"))
        self.assertIn("the sweep above", printed)
        self.assertEqual(self.read("SELECT strikes FROM sweepStrikes"),
                         [(1,)])

    def test_a_startup_sighting_of_a_tripped_run_names_the_acting_sweep(self):
        self.stale_holder(minutes_silent=12, strikes=1)

        printed = self.main_output()

        self.assertEqual(printed.count("--sweep --act"), 1)
        self.assertIn(str(self.target), printed)  # copy-pasteable hint

    def test_a_healthy_holder_prints_a_refusal_and_no_sweep_lines(self):
        """A live run at a fresh heartbeat is swept and found healthy: the
        refusal prints alone, with no strike recorded and no table."""
        self.stale_holder(minutes_silent=0)

        printed = self.main_output()

        self.assertIn("claim refused", printed)
        self.assertNotIn("swept", printed)
        self.assertNotIn("strike", printed)
        self.assertNotIn("the sweep above", printed)
        self.assertEqual(self.read("SELECT strikes FROM sweepStrikes"), [])


class CommitThenTimeout(Commit):
    """An implementer turn that commits real work, then hits the budget.

    Raises what `holophyte.agents.agent()` raises once the cap has reaped the turn:
    `TimeoutExpired` carrying the output captured before the kill.
    """

    def play(self, cwd, turn):
        super().play(cwd, turn)
        raise subprocess.TimeoutExpired("claude", 300,
                                        output="partial progress before cap")


class Boom:
    """An implementer turn that dies the way a failed `sh()` does."""

    role = "implement"

    def play(self, cwd, turn):
        raise RuntimeError("`['git', 'checkout']` failed:\nfatal: scripted")


class Refuse:
    """An implementer turn that fails the run on purpose, reason attached."""

    role = "implement"

    def play(self, cwd, turn):
        raise holophyte.gates.RunFailure("some reason")


class InfraRefuse:
    """A turn lost to the factory's own plumbing, as the reviewer route
    raises it when its container will not start."""

    role = "implement"

    def play(self, cwd, turn):
        raise holophyte.gates.InfraFailure("the reviewer container did not start")


class Interrupt:
    """An implementer turn hit by Ctrl-C."""

    role = "implement"

    def play(self, cwd, turn):
        raise KeyboardInterrupt


class MainDiverges:
    """A review turn that also lands a commit on main behind the branch.

    The one way a merge conflict happens for real: main moves while the run
    is under review, so the `--no-ff` merge at the end of the run meets a
    changed file. The step answers the review turn as usual after committing.
    """

    role = REVIEW_ROLES

    def __init__(self, commit, text=APPROVE.text):
        self.commit = commit
        self.text = text

    def play(self, cwd, turn):
        self.commit()
        return self.text


class MergeConflictTests(LoopFixture):
    """The merge gate meeting a conflict: only a conflict whose unmerged set
    is exactly FINDINGS.md is resolved, and anything else aborts the merge,
    leaves main clean and fails the run with the paths named."""

    def commit_on_main(self, path, body):
        """Land `body` at `path` on main — the divergence the merge meets."""
        file = self.target / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(body)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"main moves {path}")

    def main_status(self):
        """Main's tracked state: empty means no half-applied merge left.

        Untracked files are excluded because a failed run deliberately leaves
        its regenerated FINDINGS.md window uncommitted for a human, which is
        not merge residue.
        """
        return self.git("status", "--porcelain", "-uno").strip()

    def mid_merge(self):
        """Whether main is still sitting in a merge git never finished."""
        return (self.target / ".git" / "MERGE_HEAD").exists()

    def test_a_conflict_outside_findings_aborts_and_leaves_main_clean(self):
        self.loop(Commit("branch edit", path="README.md", body="branch side\n"),
                  MainDiverges(lambda: self.commit_on_main("README.md",
                                                           "main side\n")))

        self.assertEqual(self.main_status(), "")
        self.assertFalse(self.mid_merge())
        self.assertEqual((self.target / "README.md").read_text(), "main side\n")
        ((outcome, reason),) = self.read(
            "SELECT outcome, outcomeReason FROM runs")
        self.assertEqual(outcome, "failed")
        self.assertIn("README.md", reason)
        self.assertIn(BRANCH, self.branches())  # preserved for a human

    def test_a_conflicting_path_that_merely_contains_findings_md_is_not_resolved(self):
        """The unmerged set decides, not a substring of the merge's output: a
        conflict in `docs/FINDINGS.md-notes.md` names FINDINGS.md in every
        line git prints about it, and is still a non-FINDINGS conflict."""
        path = "docs/FINDINGS.md-notes.md"
        self.commit_on_main(path, "base\n")
        self.loop(Commit("branch edit", path=path, body="branch side\n"),
                  MainDiverges(lambda: self.commit_on_main(path, "main side\n")))

        self.assertEqual(self.main_status(), "")
        self.assertFalse(self.mid_merge())
        self.assertEqual((self.target / path).read_text(), "main side\n")
        ((outcome, reason),) = self.read(
            "SELECT outcome, outcomeReason FROM runs")
        self.assertEqual(outcome, "failed")
        self.assertIn(path, reason)

    def test_a_conflict_only_in_findings_md_still_takes_the_branch_side(self):
        """The kept resolution: main's FINDINGS.md window moves while the run
        is under review and the branch wrote its own, so the merge really
        conflicts there — and the branch's fuller window wins, merge lands."""
        self.loop(Commit("branch window", path="FINDINGS.md",
                         body="branch window\n"),
                  MainDiverges(lambda: self.commit_on_main("FINDINGS.md",
                                                           "main window\n")))

        self.assertEqual(self.main_status(), "")
        self.assertFalse(self.mid_merge())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertIn("branch window", (self.target / "FINDINGS.md").read_text())
        self.assertNotIn("main window", (self.target / "FINDINGS.md").read_text())
        self.assertNotIn(BRANCH, self.branches())  # merged, so cleaned up


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
                                    "asked_ms": beat, "level": "attention"}])

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
        holophyte.loop.approve(self.tgt, "KO-131", "ok", out=out)
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

    def test_a_shepherd_release_of_a_local_park_does_not_merge(self):
        """`--shepherd` is not an approval. `store.shepherd()` refuses a run
        parked with no pull request, but the resumed claim holds the line
        on its own: a parked local candidate whose newest intervention is
        `shepherd` (written here through the store API, the way an operator
        at the REPL rung could) is not taken through the gate -- the next
        run fails naming the release, main is untouched, the branch and
        worktree stay for `--approve`."""
        self.configure('[merge]\nmode = "local"\napprove = "human"\n')
        self.loop(Commit("the scripted work"), APPROVE,
                  provider=StubProvider(a_task()))
        with self.assertRaises(SystemExit) as refused:
            holophyte.loop.shepherd_ticket(self.tgt, "KO-131", "look again",
                                           out=io.StringIO())
        self.assertIn("no pull request", str(refused.exception))
        conn = holophyte.runs.open_store(self.tgt)
        try:
            store.record_intervention(conn, 1, "shepherd", "look again")
            store.release(conn, 1, "abandoned", "released by hand")
            conn.execute("UPDATE runs SET resumePhase = 'merge_gate'"
                         " WHERE id = 1")
            store.walk_ticket(conn, 1, "ready")
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
        self.assertIn("released by --shepherd", rows[1][3])
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
                holophyte.loop.approve(self.tgt, "KO-131", "ok",
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


class SelfHostingTests(LoopFixture):
    """A loop working on the factory's own repository re-executes itself
    after a merge, so the merged code is what runs the next pass. Through
    the `EXEC` seam: the test runner is never exec-ed."""

    def setUp(self):
        super().setUp()
        self.execs = []
        patcher = patch.object(holophyte.loop, "EXEC",
                               lambda *args: self.execs.append(args))
        patcher.start()
        self.addCleanup(patcher.stop)

    def host_the_factory_in(self, repo):
        """Make the module look imported from `repo`, the way it is when the
        target is the factory's own checkout."""
        patcher = patch.object(holophyte.loop, "__file__",
                               str(repo / "holophyte" / "loop.py"))
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

        self.assertTrue(holophyte.loop.self_hosted(target(ROOT)))
        self.assertFalse(holophyte.loop.self_hosted(target(ROOT / "holophyte")))

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



class MergeModeTests(LoopFixture):
    """`[merge] mode = "pr"`: an approved, verified candidate is pushed and
    opened as a pull request instead of merged, and the loop shepherds the
    PR -- threads verdicted, fixed and answered, checks awaited -- until it
    merges through the PR's API or the run parks. `"local"`, or no key,
    merges as it always has.

    `git` and `gh` on PATH are fakes that record their argv: the fake `git`
    intercepts `push` alone and hands everything else to the real one, so
    the loop's worktrees, merges and rev-parses are real while the one call
    that would leave the machine is witnessed instead of made. The fake
    `gh` answers `pr create` with `URL` and `api` with what the test put in
    the state files: the PR's threads and checks for the state query, an
    empty success for the reply and resolve mutations, `MERGE_SHA` for the
    merge."""

    URL = "https://github.com/example/repo/pull/7"
    # The `origin` the fixture target is given: the repository the push
    # goes to and the one `gh pr create` must be pinned to.
    ORIGIN = "https://github.com/example/repo.git"
    MERGE_SHA = "9f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"
    # A body the claim-time template gate accepts, so the run reaches the
    # gate with a ticket body for the PR to carry.
    BODY = (
        "# Add a thing\n\n## Summary\n\nThe thing, added.\n\n"
        "## What / Why / How\n\n**What:** Add the thing.\n\n"
        "**Why:** The thing is wanted.\n\n**How:** Write the thing.\n\n"
        "## In scope\n\n* The thing.\n\n## Out of scope\n\n"
        "* Everything else.\n\n## Acceptance criteria\n\n"
        "- [ ] Given the thing, when it runs, then it works (a test witnesses"
        " this)\n\n## Verify command(s)\n\n```\necho ok\n```\n\n"
        "## Implementation notes\n\n* None.\n\n"
        "## Estimate & dependencies\n\nEstimate: 5 min · Depends on: none\n\n"
        "## Open questions\n\n* None\n")
    # Two threads a review bot might leave: a clear defect and a style nit.
    DEFECT = ("src/app.py", 10, "review-bot",
              "`load()` returns None when the file is missing and the"
              " caller indexes it: a crash on first run.")
    NIT = ("src/app.py", 20, "style-bot",
           "Prefer `thing_count` over `n` for this variable name.")

    @staticmethod
    def comment(number, author, body):
        """One comment as the GraphQL answer carries it."""
        return {"author": {"login": author}, "body": body,
                "url": f"{MergeModeTests.URL}#discussion_r{number}"}

    @classmethod
    def thread(cls, number, path, line, author, body, replies=(),
               resolved=False, next_cursor=None):
        """One review thread as the GraphQL answer carries it: the opening
        comment, then `replies` (each `(author, body)`) as the follow-ups
        on its first page of comments; `next_cursor` names a further page
        the shepherd must fetch."""
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
                 head=HEAD, resolved=(), next_cursor=None):
        """The state query's answer: `threads` (each a `DEFECT`/`NIT`-shaped
        tuple) open, `resolved` the same shape but resolved, the head's
        check rollup, whether the PR is merged, and -- for a page that is
        not the last -- the cursor of the next."""
        nodes = [self.thread(n, *t) for n, t in enumerate(threads, 1)]
        nodes += [self.thread(n, *t[:4], resolved=True)
                  for n, t in enumerate(resolved, len(nodes) + 1)]
        return {"data": {"repository": {"pullRequest": {
            "state": "MERGED" if merged else "OPEN", "merged": merged,
            "headRefOid": head,
            "mergeCommit": {"oid": self.MERGE_SHA} if merged else None,
            "commits": {"nodes": [{"commit": {"statusCheckRollup":
                                              {"state": checks}}}]},
            "reviewThreads": {
                "pageInfo": {"hasNextPage": next_cursor is not None,
                             "endCursor": next_cursor},
                "nodes": nodes}}}}}

    def fake_route(self, push_exit=0, push_sh="", states=None,
                   comments=()):
        """Put a recording `git` and `gh` ahead of the real PATH, and give
        the target an `origin` for them to name.

        Each call appends its argv to `self.calls`; `gh pr create` keeps
        the body it read on stdin in `self.pr_body` and prints `URL`; each
        `gh api` call keeps its JSON body under `self.api_dir` (read back
        by `api_calls()`) and answers by what the body asks: the state
        query gets the first of `states` (each served once until the last,
        which is served forever), a mutation an empty success, the merge
        `MERGE_SHA`, a thread's further comments page the next of
        `comments` (each a `comments_page()`). `push_exit` is what `git
        push` answers with --
        non-zero is a remote refusing -- and `push_sh` is shell the fake
        push runs first, for a push that takes its time.
        """
        self.git("remote", "add", "origin", self.ORIGIN)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bindir = Path(tmp.name)
        self.calls = bindir / "calls.log"
        self.pr_body = bindir / "pr_body.md"
        self.api_dir = bindir / "api"
        self.api_dir.mkdir()
        answers = bindir / "states"
        answers.mkdir()
        for n, state in enumerate([self.pr_state()] if states is None
                                  else states, 1):
            (answers / f"{n:03d}.json").write_text(json.dumps(state))
        pages = bindir / "comments"
        pages.mkdir()
        for n, page in enumerate(comments, 1):
            (pages / f"{n:03d}.json").write_text(json.dumps(page))
        real_git = shutil.which("git")
        (bindir / "git").write_text(
            "#!/bin/sh\n"
            'if [ "$1" = push ]; then\n'
            f'  printf "git %s\\n" "$*" >> "{self.calls}"\n'
            f"{push_sh}\n"
            f'  [ {push_exit} -eq 0 ] || echo "remote: refused" >&2\n'
            f"  exit {push_exit}\n"
            "fi\n"
            f'exec "{real_git}" "$@"\n')
        (bindir / "gh").write_text(
            "#!/bin/sh\n"
            f'printf "gh %s\\n" "$*" >> "{self.calls}"\n'
            'if [ "$1" = api ]; then\n'
            f'  n=$(ls "{self.api_dir}" | wc -l); n=$((n+1))\n'
            f'  body="{self.api_dir}/$n.json"; cat > "$body"\n'
            '  if grep -q resolveReviewThread "$body"; then\n'
            "    echo '{\"data\":{\"resolveReviewThread\":{}}}'\n"
            '  elif grep -q addPullRequestReviewThreadReply "$body"; then\n'
            "    echo '{\"data\":{\"addPullRequestReviewThreadReply\":{}}}'\n"
            '  elif grep -q PullRequestReviewThread "$body"; then\n'
            f'    f=$(ls "{pages}"/*.json | head -1); cat "$f"; rm "$f"\n'
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

    def api_calls(self):
        """Every `gh api` body, in order, as `(kind, variables)`: the kind
        is `state`, `reply`, `resolve` or `merge`."""
        calls = []
        for path in sorted(self.api_dir.iterdir(),
                           key=lambda p: int(p.stem)):
            body = json.loads(path.read_text())
            query = body.get("query", "")
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

    def test_pr_pushes_opens_the_pull_request_and_parks_the_run(self):
        """Push, then create, in that order; the PR is titled `KO-n: TITLE`
        and its body is the ticket body followed by the run's FINDINGS
        entry; the shepherd's one pass finds no thread and green checks,
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
        self.assertEqual(len(calls), 3, calls)
        self.assertEqual(calls[0], f"git push origin {BRANCH}")
        # Pinned to the repository the push went to, not `gh`'s own default
        # repository (`gh repo set-default`), which can point elsewhere.
        self.assertEqual(
            calls[1],
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

    def test_a_slow_push_keeps_the_run_heartbeating(self):
        """The push and the create block for as long as the remote takes,
        outside any agent turn or verify: a push longer than the stale
        budget was a `stale_heartbeat` trip for the supervisor, which could
        fail the run before its URL was recorded. The fake push here samples
        the run's `lastHeartbeat` from the store while it takes longer than
        the whole stale budget; the beat must move under it."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       "[supervisor]\nheartbeat_stale_min = 0.01\n")
        knobs = holophyte.config.sweep_config(self.tgt)
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
        # Local main got the close-out's FINDINGS commit, as after a local
        # merge, and nothing else: the candidate landed on GitHub's main.
        self.assertEqual(self.subjects(),
                         ["Complete task KO-131: add a thing", "base"])
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
        """A thread with more comments than one page holds: the shepherd
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
        whose open thread is on the second page is not quiet. The shepherd
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
        "merge": the resumed run shepherds the PR once more and, green and
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
        holophyte.loop.approve(self.tgt, "KO-131", "looks fine",
                               out=io.StringIO())

        fake, guard = self.loop(provider=self.provider())

        self.assertEqual(fake.roles, [])
        self.assertEqual(guard.spawned, [])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge"])
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [])
        self.assertEqual(self.subjects(),
                         ["Complete task KO-131: add a thing", "base"])
        self.assertNotIn(BRANCH, self.branches())
        self.assertEqual(
            self.read("SELECT id, phase, outcome, resumePhase, prUrl,"
                      " candidateSha, mergeSha FROM runs ORDER BY id"),
            [(1, "failed", "abandoned", "merge_gate", self.URL, approved,
              None),
             (2, "done", "merged", None, None, None, self.MERGE_SHA)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])

    def test_a_shepherd_release_parks_again_rather_than_merging(self):
        """`--shepherd KO-n` is "look again", not "merge": the resumed run
        shepherds the PR and, green and quiet under `approve = "human"`,
        parks again on the same URL."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        self.loop(Commit("the scripted work"), APPROVE,
                  provider=self.provider())
        holophyte.loop.shepherd_ticket(self.tgt, "KO-131", "bots are done",
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
            self.read('SELECT "action" FROM interventions'), [("shepherd",)])

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
        knobs = holophyte.config.sweep_config(self.tgt)
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
    way `act_on_trip()` makes it -- and commits as usual.
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
                      f" (failed: {sweep_reason}); stopping", out)
        # The stream ends where the sweep ended it: no phase event after the
        # release, so nothing reanimated the run.
        self.assertEqual(self.transitions(),
                         ["claimed -> working", "working -> failed"])
        self.assertEqual(
            self.read("SELECT outcome, outcomeReason, phase FROM runs"),
            [("failed", sweep_reason, "failed")])
        # Nothing pushed to the board past the claim, nothing merged, and
        # the loop stopped on the failure.
        self.assertEqual(provider.states, [("iss-131", "In Progress")])
        self.assertEqual(self.subjects(), ["base"])
        self.assertEqual(self.rc, 1)
        # The worktree and its branch are as the implementer left them.
        self.assertIn("swept work", self.subjects("task/ko-131-add-a-thing"))
        self.assertTrue(any(p.is_dir() for p in self.worktrees.iterdir()))
        # The reviewer never ran: the script's APPROVE is still unconsumed.
        self.assertEqual([turn.role for turn in self.last_fake.turns],
                         ["implement"])
