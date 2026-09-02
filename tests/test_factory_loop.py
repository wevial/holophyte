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

import importlib.util
import io
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
SPEC = importlib.util.spec_from_file_location("holophyte_factory", ROOT / "factory.py")
factory = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(factory)

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
    no_agent_processes,
)

import store  # noqa: E402 - after the sys.path insert above

# The branch the loop cuts for the task below. Spelled out rather than derived
# from `factory`'s slug rule: an expectation computed by the code under test
# is not an expectation.
BRANCH = "task/ko-131-add-a-thing"


class StubProvider:
    """The provider seam `main()` drives, queueing tasks it hands out in order."""

    TEAM = "team-under-test"

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

    def claim_next(self, skip=()):
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
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "main").strip()

        # Where `Target.locate(self.target)` will look: the target's directory
        # under a HOLOPHYTE_HOME of this test's own, never the operator's real
        # one.
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(root / "home")})
        home.start()
        self.addCleanup(home.stop)
        self.db = factory.state_dir(self.target) / "store.db"
        self.db.parent.mkdir(parents=True)
        self.tgt = factory.Target.locate(self.target)
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
        self.tgt = factory.Target.locate(self.target)

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
                with patch.object(factory, "agent", fake):
                    self.rc = factory.main(self.tgt, provider)
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
        (_, body), = provider.comments
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

        self.assertEqual(factory.failure_history(conn, 1), [])

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

    Raises what `factory.agent()` raises once the cap has reaped the turn:
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
        raise factory.RunFailure("some reason")


class InfraRefuse:
    """A turn lost to the factory's own plumbing, as the reviewer route
    raises it when its container will not start."""

    role = "implement"

    def play(self, cwd, turn):
        raise factory.InfraFailure("the reviewer container did not start")


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


class SelfHostingTests(LoopFixture):
    """A loop working on the factory's own repository re-executes itself
    after a merge, so the merged code is what runs the next pass. Through
    the `EXEC` seam: the test runner is never exec-ed."""

    def setUp(self):
        super().setUp()
        self.execs = []
        patcher = patch.object(factory, "EXEC",
                               lambda *args: self.execs.append(args))
        patcher.start()
        self.addCleanup(patcher.stop)

    def host_the_factory_in(self, repo):
        """Make the module look imported from `repo`, the way it is when the
        target is the factory's own checkout."""
        patcher = patch.object(factory, "__file__", str(repo / "factory.py"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_merge_into_the_factory_itself_re_executes_the_loop(self):
        # The original command line, interpreter flags included: a loop
        # launched with -u must keep streaming its log after the restart.
        orig = ["/usr/bin/python3", "-u", "factory.py", "/srv/dev/holophyte"]
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
        orig = ["/usr/bin/python3", "factory.py", "/srv/dev/holophyte"]
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
        re-exec on gembox died with FileNotFoundError on exactly that. The
        program handed to the exec must be a real path; argv stays verbatim."""
        orig = ["python3", "-u", "factory.py", "/srv/dev/holophyte"]
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
