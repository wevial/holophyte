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

import dataclasses
import io
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
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
import holophyte.runs  # noqa: E402 - after the sys.path insert above
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
        # The board lease label (KO-351): the labels each issue carries now,
        # seeded from the task's `labels`; every label write in order as
        # `("label" | "unlabel", issue_id, name)` with the exact name; and
        # every read-back, as the issue asked about.
        self.labels = {task["issue_id"]: list(task.get("labels") or [])
                       for task in tasks}
        self.label_calls = []
        self.read_calls = []

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

    def ready_issues(self):
        """Every task the board would offer `claim_next()`: the queue as it
        stands, so the loop's queue mirror sees what the claim sees."""
        return [dict(task) for task in self.queue]

    def fetch_task(self, issue_id):
        """The ticket as the board holds it now; None when there is no such issue."""
        task = self.live.get(issue_id)
        return dict(task) if task else None

    def set_state(self, issue_id, state):
        self.states.append((issue_id, state))

    def comment(self, task_id, body):
        self.comments.append((task_id, body))

    def label_issue(self, issue_id, name):
        self.label_calls.append(("label", issue_id, name))
        have = self.labels.setdefault(issue_id, [])
        if name not in have:
            have.append(name)

    def issue_labels(self, issue_id):
        self.read_calls.append(issue_id)
        return list(self.labels.setdefault(issue_id, []))

    def unlabel_issue(self, issue_id, name):
        self.label_calls.append(("unlabel", issue_id, name))
        have = self.labels.setdefault(issue_id, [])
        self.labels[issue_id] = [n for n in have if n != name]

    # What the board says it has closed, identifier -> state type, when the
    # startup reconcile asks; empty means the mirror is current.
    closed = {}

    def closed_identifiers(self, identifiers):
        self.asked = list(identifiers)
        return {i: self.closed[i] for i in identifiers if i in self.closed}


def a_task(n=1):
    """One ticket in the shape `linear_provider.parse_task()` returns."""
    return {"id": f"KO-13{n}", "issue_id": f"iss-13{n}", "title": "add a thing",
            "verify": "echo ok", "budget_min": 5, "contracts": [],
            "criteria": ["Given the thing, when it runs, then it works"]}


# A body `ticket_template.validate()` accepts, in the shape the Linear
# provider hands over; the tests that route on the body use it.
VALID_BODY = (
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
# The same body with the template's own Summary placeholder left in, as
# KO-165 was claimed: criteria and a verify command present, so every
# store-side gate says `ready`, and only the validator objects.
INVALID_BODY = VALID_BODY.replace(
    "The thing, added.", "<Describe the outcome in one or two sentences.>")


class LoopFixture(unittest.TestCase):
    """The real repo, worktree directory and store every loop test runs on.

    Split from the tests so a suite with its own configuration — the
    `[worktree]` one below — reuses the fixture without re-running the tests
    that came with it.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # The GitHub budget the reconcile remembers is the process's; a
        # test that ran it low must not back off the tests after it.
        budget = patch.object(holophyte.loop, "GITHUB_BUDGET",
                              holophyte.loop.GitHubBudget())
        budget.start()
        self.addCleanup(budget.stop)
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

    def loop(self, *script, provider=None, fake=None):
        """Run `main()` over the queued tasks with the script answering agents.

        Returns the fake and the spawn guard, so a test can read both the
        turns the loop took and the processes it did not start; `main()`'s
        return code lands in `self.rc` for the tests that pin the exit
        contract. A test that needs the fake before the loop runs -- a step
        that reads the turn the loop is asking for -- builds it and passes
        it as `fake`; `script` is then unused.
        """
        fake = fake or FakeAgent(*script)
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
        real = holophyte.loop.set_phase

        def watching(conn, run_id, phase, note=None):
            (branch,) = conn.execute(
                "SELECT branch FROM runs WHERE id = ?", (run_id,)).fetchone()
            seen.append((phase, branch))
            return real(conn, run_id, phase, note)

        with patch.object(holophyte.loop, "set_phase", watching):
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


class SkipLineTests(unittest.TestCase):
    """The admit step's line for a parked ticket names why it is parked
    (KO-345): the strike-out, the pull request awaiting `--approve`, or the
    question -- so a ticket parked for the operator's merge is not reported
    as "repeated failures" that never happened."""

    def test_the_three_parks_read_as_what_they_are(self):
        struck = holophyte.loop.skip_line("KO-131", 2, None, None)
        self.assertIn("2 failures", struck)
        self.assertIn("a human owns it now", struck)

        url = "https://github.com/example/repo/pull/7"
        parked = holophyte.loop.skip_line("KO-131", 0, url,
                                          f"PR open: {url}\nready to merge")
        self.assertIn(url, parked)
        self.assertIn("--approve KO-131", parked)
        self.assertNotIn("fail", parked)

        asked = holophyte.loop.skip_line(
            "KO-131", 0, None, "merge?\nthe branch is at abc123")
        self.assertIn("a question: merge?;", asked)
        self.assertNotIn("abc123", asked)
        self.assertNotIn("fail", asked)

        closed = holophyte.loop.skip_line(
            "KO-131", 0, url, f"PR closed without merge: {url}")
        self.assertIn(f"a question: PR closed without merge: {url};", closed)
        self.assertNotIn("--approve", closed)

    def test_a_module_question_outranks_the_strike_count(self):
        """The run that parked the ticket on a merge conflict may also be
        the failure that reached the threshold. The conflict is what the
        operator has to resolve, so it is the line -- and since KO-365 the
        line names the way back, `--requeue`; the escalation's own
        question is the one park the count speaks for."""
        conflicted = holophyte.loop.skip_line(
            "KO-131", 2, None,
            "merge conflict with main on: README.md; resolve it on the branch")
        self.assertIn("parked on a merge-gate conflict; resolve the branch"
                      " and --requeue KO-131", conflicted)
        self.assertNotIn("struck out", conflicted)
        self.assertNotIn("a question", conflicted)

        struck = holophyte.loop.skip_line(
            "KO-131", 2, None, holophyte.board.strike_question(2))
        self.assertIn("struck out after 2 failures", struck)
        self.assertNotIn("a question", struck)


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

    # --- the reuse merge stopping on conflicts (KO-355) ------------------

    TEST_FILE = "tests/test_thing.py"
    BOTH_TESTS = ("def test_it_works():\n    pass\n\n"
                  "def test_branch_side():\n    pass\n\n"
                  "def test_main_side():\n    pass\n")

    def conflicting_leftover(self):
        """A preserved branch and a main that both append a test at the same
        lines of the same file: the add/add overlap the operator resolved
        three times in one day."""
        wt = self.leftover()
        (wt / self.TEST_FILE).write_text(
            "def test_it_works():\n    pass\n\ndef test_branch_side():\n"
            "    pass\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", "rescued: the branch's test", cwd=wt)
        (self.target / self.TEST_FILE).write_text(
            "def test_it_works():\n    pass\n\ndef test_main_side():\n"
            "    pass\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "main moved on with its own test")
        return wt

    def test_a_conflicting_reuse_is_handed_to_the_implementer_who_resolves_it(self):
        """The conflict is the implementer's first commit, not a person's
        park: the brief opens by naming the path, and the run reaches its
        first verify with the merge committed -- MERGE_HEAD gone."""
        self.conflicting_leftover()
        resolve = ResolveMerge(self.TEST_FILE, self.BOTH_TESTS)
        review = ApproveNotingMergeHead()

        fake, _ = self.loop(resolve, review)

        self.assertEqual(fake.roles, ["implement", "review"])
        brief = fake.turns[0].goal
        self.assertTrue(brief.startswith("FIRST, before the ticket's work"),
                        brief[:200])
        self.assertIn(self.TEST_FILE, brief)
        self.assertLess(brief.index(self.TEST_FILE),
                        brief.index("Implement this task"))
        self.assertIn(self.TEST_FILE, resolve.conflicted)
        self.assertFalse(review.mid_merge)
        # The resolution and the ticket's work both reached main.
        self.assertEqual((self.target / self.TEST_FILE).read_text(),
                         self.BOTH_TESTS)
        self.assertIn("Merge main into the preserved branch: both tests",
                      self.subjects())
        self.assertIn("rescued: the branch's test", self.subjects())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_an_unresolved_reuse_merge_fails_at_the_first_verify(self):
        """An implementer that commits nothing leaves the tree mid-merge;
        the first verify fails the run naming the unresolved merge, no
        reviewer is asked, and the branch keeps its preserved commit."""
        self.conflicting_leftover()
        moved_main = self.git("rev-parse", "main").strip()

        fake, _ = self.loop(Idle())

        self.assertEqual(fake.roles, ["implement"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("left the merge unresolved", reason)
        self.assertIn(self.TEST_FILE, reason)
        self.assertIn("a human resolves the merge", reason)
        self.assertIn(BRANCH, self.branches())
        self.assertIn("rescued: the branch's test", self.subjects(BRANCH))
        self.assertEqual(self.git("rev-parse", "main").strip(), moved_main)

    def test_a_reuse_that_merges_main_cleanly_carries_no_conflict_paragraph(self):
        """Main moved on in a different file: the merge lands on its own and
        the implementer is briefed on the ticket alone."""
        wt = self.leftover()
        (wt / "work.txt").write_text("preserved\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", "rescued: preserved work", cwd=wt)
        (self.target / "new.txt").write_text("newer main\n")
        self.git("add", "new.txt")
        self.git("commit", "-q", "-m", "main moved on")

        fake, _ = self.loop(Commit("the scripted work"), APPROVE)

        brief = fake.turns[0].goal
        self.assertTrue(brief.startswith("Implement this task"), brief[:200])
        self.assertNotIn("mid-merge", brief)
        self.assertNotIn("conflict", brief)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

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
        project = store.ensure_project(conn, StubProvider.TEAM,
                                       str(self.target))
        ticket = store.mirror_ticket(
            conn, project, linear_issue_id="iss-stale",
            linear_identifier=self.HELD, title="stalled elsewhere",
            acceptance_criteria=["Given a run, then it heartbeats"],
            verification_commands=["echo ok"], time_box_ms=25 * self.MINUTE)
        store.transition(conn, ticket, "in_flight")
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
        project = store.ensure_project(conn, StubProvider.TEAM, str(self.target))
        runs = {}
        for n in (1, 2):
            ticket = store.mirror_ticket(
                conn, project, linear_issue_id=f"iss-{n}",
                linear_identifier=f"KO-{n}", title=f"ticket {n}",
                acceptance_criteria=self.CRITERIA,
                verification_commands=["echo ok"])
            runs[f"KO-{n}"] = store.claim(conn, project, ticket)
            store.transition(conn, ticket, "in_flight")
            store.release(conn, runs[f"KO-{n}"], "failed", "crashed")
            store.requeue(conn, ticket, "contract fixed")
        store.mirror_ticket(conn, project, linear_issue_id="iss-2",
                            linear_identifier="KO-2", title="ticket 2")
        store.mirror_ticket(conn, project, linear_issue_id="iss-3",
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
        project = store.ensure_project(conn, StubProvider.TEAM, str(self.target))
        ticket = store.mirror_ticket(
            conn, project, linear_issue_id="iss-9", linear_identifier="KO-9",
            title="being worked", acceptance_criteria=self.CRITERIA,
            verification_commands=["echo ok"])
        run_id = store.claim(conn, project, ticket)
        store.transition(conn, ticket, "in_flight")
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
                project = store.ensure_project(other, team, target)
                (ticket_id,) = other.execute(
                    "SELECT id FROM tickets WHERE linearIdentifier = 'KO-1'"
                ).fetchone()
                self.run_id = store.claim(other, project, ticket_id)
                store.transition(other, ticket_id, "in_flight")
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
        other = store.ensure_project(conn, "another-team", "/elsewhere")
        store.mirror_ticket(conn, other, linear_issue_id="iss-x",
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
        project = store.ensure_project(conn, StubProvider.TEAM, str(self.target))
        ticket = holophyte.board.mirror_task(conn, project, c)
        store.transition(conn, ticket, "blocked_on_deps")
        store.transition(conn, ticket, "blocked_on_operator")
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

@dataclasses.dataclass
class ResolveMerge:
    """An implementer turn on a worktree left mid-merge: it records the paths
    git says are unmerged, writes `resolved` to `path`, commits the merge
    with a message naming both sides, then does one scripted commit of the
    ticket's own work."""

    path: str
    resolved: str
    conflicted: list = dataclasses.field(default_factory=list)

    role = "implement"

    def play(self, cwd, turn):
        self.conflicted = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"], cwd=cwd,
            capture_output=True, text=True).stdout.split()
        (cwd / self.path).write_text(self.resolved)
        self.git(cwd, "add", self.path)
        self.git(cwd, "commit", "-q", "-m",
                 "Merge main into the preserved branch: both tests")
        return Commit("the scripted work").play(cwd, turn)

    @staticmethod
    def git(cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, check=True,
                       capture_output=True, text=True)


class ApproveNotingMergeHead:
    """The approval, recording whether the worktree was still mid-merge when
    the review turn arrived -- the state the first verify must have seen."""

    role = APPROVE.role
    mid_merge = None

    def play(self, cwd, turn):
        self.mid_merge = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=cwd,
            capture_output=True).returncode == 0
        return APPROVE.play(cwd, turn)


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
    """The merge gate meeting a conflict: a `main` that conflicts with the
    branch, on any path, goes to the implementer first (KO-404); a merge
    it leaves unresolved is aborted, main left clean and the run parked
    with the paths named. The `Idle()` step is that turn declining to
    resolve."""

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
                                                           "main side\n")),
                  Idle())

        self.assertEqual(self.main_status(), "")
        self.assertFalse(self.mid_merge())
        self.assertEqual((self.target / "README.md").read_text(), "main side\n")
        ((outcome, reason),) = self.read(
            "SELECT outcome, outcomeReason FROM runs")
        self.assertEqual(outcome, "failed")
        self.assertIn("README.md", reason)
        self.assertIn(BRANCH, self.branches())  # preserved for a human

    def test_a_conflict_with_main_parks_the_ticket_and_moves_nothing(self):
        """KO-342: the gate merges main into the branch first, and a
        conflict the implementer leaves unresolved is a person's: the
        ticket is blocked with the path in its question, the branch sits
        at its pre-gate sha, main is where the divergence left it."""
        seen = {}

        def diverge():
            self.commit_on_main("README.md", "main side\n")
            seen["main"] = self.git("rev-parse", "main").strip()
            seen["branch"] = self.git("rev-parse", BRANCH).strip()

        self.loop(Commit("branch edit", path="README.md", body="branch side\n"),
                  MainDiverges(diverge), Idle())

        ((status, question),) = self.read(
            "SELECT status, blockedQuestion FROM tickets")
        self.assertEqual(status, "blocked_on_operator")
        self.assertIn("README.md", question)
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), seen["branch"])
        self.assertEqual(self.git("rev-parse", "main").strip(), seen["main"])
        self.assertEqual(self.main_status(), "")

    def test_a_conflict_park_after_a_strike_is_reported_as_the_conflict(self):
        """KO-345 review: one failed run, then a run the gate parks on a
        merge conflict -- a second counted failure. The next pass names the
        conflict, which is what the operator must resolve, not a strike-out
        that would send them looking for a failure of the work."""
        self.loop(Commit("first cut"), REQUEST_CHANGES,
                  Commit("first fix round 1"), REQUEST_CHANGES,
                  Commit("first fix round 2"), FAIL)
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        store.walk_ticket(conn, 1, "ready")
        self.loop(Commit("branch edit", path="README.md", body="branch side\n"),
                  MainDiverges(lambda: self.commit_on_main("README.md",
                                                           "main side\n")),
                  Idle())
        self.assertEqual(
            self.read("SELECT outcome FROM runs ORDER BY id"),
            [("failed",), ("failed",)])
        parked, other = a_task(), dict(a_task(2), title="add another thing")

        out = self.main_output(Commit("the other work"), APPROVE,
                               provider=StubProvider(parked, other))

        self.assertIn("[holo2] KO-131 is parked on a merge-gate conflict;"
                      " resolve the branch and --requeue KO-131;", out)
        self.assertNotIn("struck out", out)
        self.assertIn("the other work", self.subjects())
        self.assertEqual(
            self.read("SELECT linearIdentifier, status FROM tickets"
                      " ORDER BY id"),
            [("KO-131", "blocked_on_operator"), ("KO-132", "merged")])

    def test_a_main_that_moved_without_conflict_is_merged_in_and_re_verified(self):
        """KO-342: main gains an unrelated commit under review; the gate
        merges it into the branch, runs the verify once more on the result,
        and the `--no-ff` merge lands with that commit behind it."""
        log = self.target.parent / "verify.log"
        task = a_task()
        task["verify"] = f"echo ran >> {log} && echo ok"
        moved = {}

        def diverge():
            self.commit_on_main("other.txt", "elsewhere\n")
            moved["sha"] = self.git("rev-parse", "main").strip()

        self.loop(Commit("branch edit", path="README.md", body="branch side\n"),
                  MainDiverges(diverge), provider=StubProvider(task))

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        # one verify before review round 1, one more at the gate
        self.assertEqual(log.read_text().splitlines(), ["ran", "ran"])
        # The --no-ff merge commit, wherever the close-out's FINDINGS commit
        # has since put main's HEAD.
        merge = self.git("log", "main", "--merges", "-1", "--format=%H").strip()
        self.assertTrue(merge, self.subjects())
        branch_tip = self.git("rev-parse", f"{merge}^2").strip()
        # The branch side of the --no-ff merge already contains main's move:
        # the gate merged it in (`git` exits nonzero, and the fixture raises,
        # when it is not an ancestor).
        self.git("merge-base", "--is-ancestor", moved["sha"], branch_tip)
        self.assertNotEqual(branch_tip, moved["sha"])
        self.assertIn("main moves other.txt", self.subjects(merge))
        self.assertEqual((self.target / "README.md").read_text(), "branch side\n")
        self.assertEqual((self.target / "other.txt").read_text(), "elsewhere\n")
        self.assertNotIn(BRANCH, self.branches())

    def test_a_conflicting_path_that_merely_contains_findings_md_is_not_resolved(self):
        """The unmerged set decides, not a substring of the merge's output: a
        conflict in `docs/FINDINGS.md-notes.md` names FINDINGS.md in every
        line git prints about it, and is still a non-FINDINGS conflict."""
        path = "docs/FINDINGS.md-notes.md"
        self.commit_on_main(path, "base\n")
        self.loop(Commit("branch edit", path=path, body="branch side\n"),
                  MainDiverges(lambda: self.commit_on_main(path, "main side\n")),
                  Idle())

        self.assertEqual(self.main_status(), "")
        self.assertFalse(self.mid_merge())
        self.assertEqual((self.target / path).read_text(), "main side\n")
        ((outcome, reason),) = self.read(
            "SELECT outcome, outcomeReason FROM runs")
        self.assertEqual(outcome, "failed")
        self.assertIn(path, reason)

    def test_a_conflict_only_in_findings_md_parks_like_any_other(self):
        """KO-342: the `--no-ff` merge used to take the branch side of a
        FINDINGS.md-only conflict, but the gate's merge of main into the
        branch grants no such exception -- a conflict the implementer
        leaves unresolved parks, with the path named, and moves nothing."""
        self.configure('[report]\nfindings = "repo"\n')
        seen = {}

        def diverge():
            self.commit_on_main("FINDINGS.md", "main window\n")
            seen["main"] = self.git("rev-parse", "main").strip()
            seen["branch"] = self.git("rev-parse", BRANCH).strip()

        self.loop(Commit("branch window", path="FINDINGS.md",
                         body="branch window\n"),
                  MainDiverges(diverge), Idle())

        ((status, question),) = self.read(
            "SELECT status, blockedQuestion FROM tickets")
        self.assertEqual(status, "blocked_on_operator")
        self.assertIn("FINDINGS.md", question)
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), seen["branch"])
        self.assertEqual(self.git("rev-parse", "main").strip(), seen["main"])
        self.assertFalse(self.mid_merge())
        # The only dirt on main is the close-out's regenerated FINDINGS.md
        # window, which every failed run leaves for a human -- here over a
        # tracked file, so it shows as modified rather than untracked.
        self.assertEqual(self.main_status(), "M FINDINGS.md")
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(self.git("show", "main:FINDINGS.md"), "main window\n")


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

    def test_the_resumed_run_records_its_branch_before_the_gate(self):
        """The resumed run reuses the candidate's worktree rather than
        cutting one, and still names the branch before its first phase
        change: the files panel reads `runs.branch` to find the worktree
        on this path as on a fresh cut."""
        self.configure('[merge]\napprove = "human"\n')
        self.loop(Commit("the scripted work"), APPROVE)
        holophyte.loop.approve(self.tgt, "KO-131", "ok", out=io.StringIO())
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
        """`--babysit` is not an approval. `store.shepherd()` refuses a run
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
            holophyte.loop.babysit_ticket(self.tgt, "KO-131", "look again",
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
    opened as a pull request instead of merged, and the loop babysits the
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
                "url": f"{MergeModeTests.URL}#discussion_r{number}"}

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
        `comments` (each a `comments_page()`), the reconcile's pull-status
        read (KO-359) an open pull request; the check-runs and
        branch-rules reads answer no runs and no rules, so the rollup
        alone decides the checks. `push_exit` is what `git
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
            f'  [ {push_exit} -eq 0 ] || echo "remote: refused" >&2\n'
            f"  exit {push_exit}\n"
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
        """Every `gh api` body the babysitter made, in order, as `(kind,
        variables)`: the kind is `state`, `reply`, `resolve` or `merge`.
        The loop's per-pass pull-status read of a parked run (KO-359) is
        left out: it is the reconcile's, tested on its own below, and
        every pass after a park makes one."""
        calls = []
        for path in sorted(self.api_dir.iterdir(),
                           key=lambda p: int(p.stem)):
            body = json.loads(path.read_text())
            query = body.get("query", "")
            if "mergedBy" in query:
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
        # The sixth is the park reading the pull request once more, after
        # the pass's own writes, for the activity mark it records (KO-362);
        # the seventh is the pass after the park asking GitHub whether the
        # parked pull request has been merged (KO-359).
        self.assertEqual(len(calls), 7, calls)
        self.assertEqual(calls[5:], ["gh api --hostname github.com --method"
                                     " POST graphql --input -"] * 2)
        self.assertEqual(calls[0], f"git push origin {BRANCH}")
        # Beside the state query: the head's check runs and main's rules,
        # so a rollup that says success before the checks have reported is
        # not read as green.
        tip = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(calls[3:5], [
            "gh api --hostname github.com --method GET"
            f" repos/example/repo/commits/{tip}/check-runs?per_page=100",
            "gh api --hostname github.com --method GET"
            " repos/example/repo/rules/branches/main"])
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
        holophyte.loop.babysit_ticket(self.tgt, "KO-131", "look again",
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
        holophyte.loop.babysit_ticket(self.tgt, "KO-131", "nit closed",
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
        holophyte.loop.babysit_ticket(self.tgt, "KO-131", "look again",
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
        holophyte.loop.babysit_ticket(self.tgt, "KO-131", "nit closed",
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
        holophyte.loop.approve(self.tgt, "KO-131", "looks fine",
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
        holophyte.loop.babysit_ticket(self.tgt, "KO-131", "bots are done",
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


    # What GitHub says about a parked pull request when the reconcile asks
    # (`pr.PULL_QUERY`'s node): merged by a coworker, closed unmerged, open.
    MERGED_PULL = {"state": "MERGED", "merged": True,
                   "mergeCommit": {"oid": MERGE_SHA},
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
        `--babysit` would -- a `shepherd` intervention, the run ended
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
            [("shepherd", "supervisor")])
        self.assertEqual(
            self.read("SELECT summary FROM runEvents"
                      " WHERE kind = 'intervention'"),
            [(f"supervisor shepherd: new review activity on {self.URL}:"
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
                         (holophyte.loop.WORKER_MERGED, None)])
        out = io.StringIO()
        with patch.object(holophyte.loop, "SPAWN", pool.spawn), \
                patch.object(holophyte.loop, "WAIT", pool.wait), \
                patch.object(sys, "stdout", out):
            rc = holophyte.loop.main(self.tgt, provider)

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


class GateConflictRequeueTests(LoopFixture):
    """A candidate parked on a merge-gate conflict can be requeued once the
    operator resolves the merge (KO-365).

    The gate's merge of `main` into the branch conflicts: the run fails and
    the ticket parks `blocked_on_operator` with the branch preserved. Before
    this, the way back was the store: `--requeue` refused a ticket that was
    not `in_flight` and `--repoint` a run that was not parked awaiting
    approval. The park here is the loop's own, made by a real conflict.
    """

    def park_on_conflict(self, provider):
        """KO-131 parked by `_sync_main_into_branch()` on a README conflict;
        returns `(ticket_id, run_id)`."""
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
        try:
            project = store.ensure_project(conn, StubProvider.TEAM,
                                           str(self.target))
            ticket = holophyte.board.mirror_task(conn, project, a_task())
            run_id = store.claim(conn, project, ticket)
            store.transition(conn, ticket, "in_flight")
            store.set_branch(conn, run_id, branch)
            # The conflict goes to the implementer first now (KO-404);
            # this fake leaves it unresolved, so the park below is the
            # same one it always was.
            with patch.object(holophyte.loop, "agent", FakeAgent(Idle())):
                with self.assertRaises(holophyte.gates.RunFailure) as failed:
                    holophyte.loop._sync_main_into_branch(
                        self.tgt, conn, run_id, provider, "KO-131", branch,
                        wt, sha, 60, "add a thing", 5)
            holophyte.board.close_out_failure(
                self.tgt, conn, run_id, ticket, reason=str(failed.exception),
                provider=provider, refresh=False)
        finally:
            conn.close()
        return ticket, run_id

    def test_a_gate_conflict_park_is_requeued_with_its_note(self):
        provider = StubProvider(a_task())
        ticket, run_id = self.park_on_conflict(provider)
        self.assertEqual(
            self.read("SELECT status, activeRunId FROM tickets"),
            [("blocked_on_operator", None)])
        self.assertIn("merge conflict with main on: README.md",
                      self.read("SELECT blockedQuestion FROM tickets")[0][0])
        self.assertIn("README.md", self.read(
            "SELECT outcomeReason FROM runs WHERE outcome = 'failed'")[0][0])

        out = io.StringIO()
        holophyte.loop.requeue(self.tgt, "KO-131",
                               "resolved README.md on the branch", out)

        self.assertEqual(out.getvalue().strip(),
                         f"[holo2] KO-131 requeued after run {run_id}")
        self.assertEqual(
            self.read("SELECT status, blockedQuestion, activeRunId"
                      " FROM tickets"),
            [("ready", None, None)])
        self.assertEqual(
            self.read("SELECT runId, action FROM interventions"),
            [(run_id, "requeue")])
        noted = self.read(
            "SELECT summary FROM runEvents WHERE summary LIKE"
            " '%resolved README.md on the branch%'")
        self.assertEqual(len(noted), 1, noted)

    def test_a_pull_request_park_is_still_refused(self):
        provider = StubProvider(a_task())
        url = "https://github.com/example/repo/pull/7"
        conn = store.open(str(self.db))
        try:
            project = store.ensure_project(conn, StubProvider.TEAM,
                                           str(self.target))
            ticket = holophyte.board.mirror_task(conn, project, a_task())
            run_id = store.claim(conn, project, ticket)
            store.transition(conn, ticket, "in_flight")
            store.park(conn, run_id, "awaiting_merge_approval",
                       pr_url=url, candidate_sha="a" * 40)
            self.assertTrue(holophyte.board.block_ticket(
                conn, ticket, provider, f"PR open: {url}"))
        finally:
            conn.close()

        with self.assertRaises(SystemExit) as refused:
            holophyte.loop.requeue(self.tgt, "KO-131", "why not",
                                   io.StringIO())

        self.assertEqual(
            str(refused.exception),
            "[holo2] KO-131 is blocked_on_operator, not in_flight; nothing"
            " to requeue")
        self.assertEqual(
            self.read("SELECT status, blockedQuestion FROM tickets"),
            [("blocked_on_operator", f"PR open: {url}")])
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"),
                         [(0,)])


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
        project = store.ensure_project(conn, StubProvider.TEAM,
                                       str(self.target))
        ticket = holophyte.board.mirror_task(conn, project, a_task())
        run_id = store.claim(conn, project, ticket)
        store.transition(conn, ticket, "in_flight")
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
        self.assertEqual(holophyte.loop.merge_conflicts(wt), [])
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
        project = store.ensure_project(conn, StubProvider.TEAM,
                                       str(self.target))
        ticket = holophyte.board.mirror_task(conn, project, a_task())
        run_id = store.claim(conn, project, ticket)
        store.transition(conn, ticket, "in_flight")
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


class BoardLeaseLabelTests(LoopFixture):
    """The claim's second lease (KO-351): a `holo:HOST` label on the
    issue, which a writer with a store of its own can see where it cannot
    see this store's `activeRunId`."""

    HOST = "writer-1"

    def setUp(self):
        super().setUp()
        self.configure('[report]\nhost_label = "writer-1"\n')

    @staticmethod
    def label(host="writer-1"):
        return f"holo:{host}"

    def labelled(self, provider, labels, *more):
        task = a_task()
        task["labels"] = labels
        return provider(task, *more)

    def seed_ended_run(self, requeue=True):
        """A run of this store on KO-131 that ended `failed` -- with the
        board down, so its label is still on the issue -- and, unless told
        otherwise, the requeue that put the ticket back. Returns the run
        id."""
        conn = store.open(str(self.db))
        try:
            project = store.ensure_project(conn, StubProvider.TEAM,
                                           str(self.target))
            ticket = holophyte.board.mirror_task(conn, project, a_task())
            run_id = store.claim(conn, project, ticket)
            store.transition(conn, ticket, "in_flight")
            store.release(conn, run_id, "failed", "the board was down")
            if requeue:
                store.requeue(conn, ticket, "board back")
            conn.commit()
        finally:
            conn.close()
        return run_id

    def test_a_claim_labels_before_the_implementer_and_a_merge_unlabels(self):
        """The label `holo:writer-1` is on the issue when the implementer's
        turn begins -- not after it, when a second writer's listing could
        already have offered the ticket -- and the merge's close-out takes
        it off."""
        provider = StubProvider(a_task())
        at_implement = []

        @dataclass
        class Witness(Commit):
            def play(self, cwd, turn):
                at_implement.append(list(provider.labels["iss-131"]))
                return super().play(cwd, turn)

        self.loop(Witness("the scripted work"), APPROVE, provider=provider)

        self.assertEqual(self.read("SELECT id, outcome FROM runs"),
                         [(1, "merged")])
        self.assertEqual(at_implement, [["holo:writer-1"]])
        self.assertEqual(provider.labels["iss-131"], [])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])
        # One read-back, between the add and the implementer.
        self.assertEqual(provider.read_calls, ["iss-131"])

    def test_a_ticket_another_writer_labelled_is_skipped_and_nothing_is_leased(self):
        provider = self.labelled(StubProvider, [self.label("writer-2")])

        out = self.main_output(Commit("never reached"), APPROVE,
                               provider=provider)

        self.assertIn("[holo2] KO-131 is leased by writer-2 on the board;"
                      " skipping it", out)
        self.assertEqual(self.read("SELECT id FROM runs"), [])
        self.assertEqual(self.last_fake.turns, [])
        self.assertEqual(provider.label_calls, [])
        self.assertEqual(provider.read_calls, [])
        self.assertEqual(provider.labels["iss-131"], ["holo:writer-2"])

    def test_this_writers_labels_with_no_live_run_are_stale_and_removed(self):
        """Run 1 ended with the board down and its label stayed. It is no
        lease: the store has no live run under it, so the claim goes ahead
        -- the stale label off under the store lease, then the fresh one
        on, then off again at the merge."""
        self.seed_ended_run()
        provider = self.labelled(StubProvider, [self.label()])

        out = self.main_output(Commit("the scripted work"), APPROVE,
                               provider=provider)

        self.assertIn("carries this writer's lease label holo:writer-1"
                      " with no live run; removing the stale label and"
                      " claiming", out)
        self.assertEqual(self.read("SELECT id, outcome FROM runs ORDER BY id"),
                         [(1, "failed"), (2, "merged")])
        self.assertEqual(provider.label_calls,
                         [("unlabel", "iss-131", "holo:writer-1"),
                          ("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.labels["iss-131"], [])
        self.assertEqual(self.last_fake.roles, ["implement", "review"])

    def test_a_stale_label_seen_by_two_loops_is_touched_only_by_the_claim_holder(self):
        """Two loops on one store admit the same stale label. The one whose
        `store.claim()` lands takes the stale one off and writes its own;
        the other reaches its claim a moment later -- here, at the instant
        the first is writing its label -- waits its turn, and is refused
        by the store before it has touched the board. The loser's
        `_claim_run()` is the real one, on a thread of its own as a
        sibling loop's would be: the witness is that the provider saw no
        call from it at all."""
        self.seed_ended_run()
        db, target, tgt = self.db, self.target, self.tgt
        seen = type("Seen", (), {"trips": (), "watched": ()})()
        competitor = []
        threads = []

        def compete(provider, issue_id):
            conn = store.open(str(db))
            try:
                project = store.ensure_project(conn, StubProvider.TEAM,
                                               str(target))
                (ticket_id,) = conn.execute(
                    "SELECT id FROM tickets WHERE linearIssueId = ?",
                    (issue_id,)).fetchone()
                competitor.append(holophyte.loop._claim_run(
                    tgt, conn, project, provider, a_task(), ticket_id, seen))
            finally:
                conn.close()

        class Contended(StubProvider):
            def label_issue(self, issue_id, name):
                if not threads:
                    thread = threading.Thread(target=compete,
                                              args=(self, issue_id))
                    threads.append(thread)
                    thread.start()
                    # The competitor is at its claim and stays there: the
                    # turn is this claim's until its label is written.
                    thread.join(0.5)
                    self.assertion = (thread.is_alive(), list(competitor))
                super().label_issue(issue_id, name)

        provider = self.labelled(Contended, [self.label()])
        out = self.main_output(Commit("the scripted work"), APPROVE,
                               provider=provider)
        threads[0].join(10)

        self.assertEqual(provider.assertion, (True, []))
        self.assertEqual(competitor, [holophyte.loop.HELD])
        self.assertIn("lease already held by run 2; skipping it", out)
        self.assertEqual(self.read("SELECT id, outcome FROM runs ORDER BY id"),
                         [(1, "failed"), (2, "merged")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        # Every board call is the winner's; the loser made none.
        self.assertEqual(provider.label_calls,
                         [("unlabel", "iss-131", "holo:writer-1"),
                          ("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.labels["iss-131"], [])

    def test_a_late_close_out_leaves_the_label_a_fresh_claim_re_asserted(self):
        """The label names the writer, not the run, so the store decides
        whether a close-out may take it off: run 1's release, arriving
        after run 2 has claimed the same ticket and re-asserted the label,
        finds the store naming run 2 as the live one and leaves the label
        on; run 2's own release takes it off."""
        ended = self.seed_ended_run()
        conn = store.open(str(self.db))
        try:
            project = store.ensure_project(conn, StubProvider.TEAM,
                                           str(self.target))
            (ticket_id,) = conn.execute("SELECT id FROM tickets").fetchone()
            live = store.claim(conn, project, ticket_id)
            conn.commit()
            provider = StubProvider(a_task())
            provider.label_issue("iss-131", "holo:writer-1")

            holophyte.board.release_lease_label(self.tgt, conn, ticket_id,
                                                provider, ended)
            self.assertEqual(provider.labels["iss-131"], ["holo:writer-1"])

            holophyte.board.release_lease_label(self.tgt, conn, ticket_id,
                                                provider, live)
            self.assertEqual(provider.labels["iss-131"], [])
        finally:
            conn.close()

    def test_a_close_out_racing_a_fresh_claim_cannot_strip_the_fresh_label(self):
        """Run 1's close-out looks at the store, finds no live run, and
        goes to the board -- and at that instant a sibling loop on this
        store reaches its claim of the same ticket. Were the claim to land
        in the gap, the removal would take the fresh run's label off and
        another writer could claim a ticket this store is working (review
        finding P1 on KO-351). The claim waits the close-out's turn
        instead: while the removal is in flight the store still names no
        live run, and the fresh claim's label is written after the removal
        and stays on the board."""
        ended = self.seed_ended_run()
        db, target, tgt = self.db, self.target, self.tgt
        seen = type("Seen", (), {"trips": (), "watched": ()})()
        claimed = []
        threads = []
        during = []

        def claim(provider, issue_id):
            conn = store.open(str(db))
            try:
                project = store.ensure_project(conn, StubProvider.TEAM,
                                               str(target))
                (ticket_id,) = conn.execute(
                    "SELECT id FROM tickets WHERE linearIssueId = ?",
                    (issue_id,)).fetchone()
                claimed.append(holophyte.loop._claim_run(
                    tgt, conn, project, provider, a_task(), ticket_id, seen))
            finally:
                conn.close()

        class Racing(StubProvider):
            def unlabel_issue(self, issue_id, name):
                if not threads:
                    thread = threading.Thread(target=claim,
                                              args=(self, issue_id))
                    threads.append(thread)
                    thread.start()
                    thread.join(0.5)
                    peek = sqlite3.connect(db)
                    try:
                        during.append(peek.execute(
                            "SELECT activeRunId FROM tickets").fetchall())
                    finally:
                        peek.close()
                super().unlabel_issue(issue_id, name)

        provider = Racing(a_task())
        provider.label_issue("iss-131", "holo:writer-1")
        conn = store.open(str(self.db))
        try:
            (ticket_id,) = conn.execute("SELECT id FROM tickets").fetchone()
            holophyte.board.release_lease_label(self.tgt, conn, ticket_id,
                                                provider, ended)
        finally:
            conn.close()
        threads[0].join(10)

        # No live run while the removal was in flight: the claim waited.
        self.assertEqual(during, [[(None,)]])
        self.assertEqual(claimed, [2])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(2,)])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1"),
                          ("label", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.labels["iss-131"], ["holo:writer-1"])

    def test_a_foreign_label_on_read_back_backs_off_removing_only_our_own_label(self):
        """The admission check reads the listing; the read-back reads the
        issue as it is. A `holo:writer-2` that landed in between is
        writer-2's lease: this writer's own label comes off and nothing
        else on the issue does, the store lease goes back without a
        strike, no run starts, and the loop moves on to the next ticket."""
        class Raced(StubProvider):
            def label_issue(self, issue_id, name):
                # writer-2 labels KO-131 after this writer's listing and
                # before its write; the read-back has it.
                if issue_id == "iss-131":
                    self.labels[issue_id].append("holo:writer-2")
                super().label_issue(issue_id, name)

        provider = self.labelled(Raced, ["other"], a_task(2))
        out = self.main_output(Commit("the scripted work"), APPROVE,
                               provider=provider)

        self.assertIn("[holo2] KO-131 is leased by writer-2 on the board;"
                      " skipping it", out)
        self.assertEqual(provider.labels["iss-131"], ["other", "holo:writer-2"])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1"),
                          ("label", "iss-132", "holo:writer-1"),
                          ("unlabel", "iss-132", "holo:writer-1")])
        self.assertEqual(
            self.read("SELECT t.linearIdentifier, r.outcome, r.outcomeClass"
                      " FROM runs r JOIN tickets t ON t.id = r.ticketId"
                      " ORDER BY r.id"),
            [("KO-131", "failed", "infra"), ("KO-132", "merged", "work")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"),
                         [(None,), (None,)])
        # Only KO-132 was worked: one implement and one review turn.
        self.assertEqual(self.last_fake.roles, ["implement", "review"])
        self.assertEqual(provider.labels["iss-132"], [])

    def test_a_label_the_board_refuses_releases_the_lease_and_starts_no_run(self):
        """The add itself raised. The store lease goes back and no run
        starts; the removal that follows is best-effort, since a raise is
        not proof that nothing landed (review finding P2 on KO-351)."""
        class Refusing(StubProvider):
            def label_issue(self, issue_id, name):
                self.label_calls.append(("label", issue_id, name))
                raise RuntimeError("linear is down")

        provider = Refusing(a_task())
        out = self.main_output(Commit("never reached"), APPROVE,
                               provider=provider)

        self.assertIn("did not take the lease label holo:writer-1", out)
        self.assertEqual(self.last_fake.turns, [])
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.read_calls, [])
        self.assertEqual(self.branches(), ["main"])

    def test_an_add_that_landed_before_it_raised_is_taken_off_again(self):
        """The mutation applied and the response failed -- a timeout after
        the write. The claim is refused as before, and the label the board
        does hold comes off, so a refused claim does not leave a lease
        every other writer will honour forever."""
        class Landed(StubProvider):
            def label_issue(self, issue_id, name):
                super().label_issue(issue_id, name)
                raise RuntimeError("linear timed out after the write")

        provider = self.labelled(Landed, ["other"])
        out = self.main_output(Commit("never reached"), APPROVE,
                               provider=provider)

        self.assertIn("did not take the lease label holo:writer-1", out)
        self.assertEqual(self.last_fake.turns, [])
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        self.assertEqual(provider.labels["iss-131"], ["other"])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])

    def test_a_read_back_the_board_refuses_ends_with_our_label_off_and_no_lease(self):
        """The add landed and the read-back raised. The store lease goes
        back as before; this writer's label goes with it -- and only that
        one: the issue's other labels are not touched."""
        class HalfTaken(StubProvider):
            def issue_labels(self, issue_id):
                self.read_calls.append(issue_id)
                raise RuntimeError("linear timed out on the read-back")

        provider = self.labelled(HalfTaken, ["other"])
        out = self.main_output(Commit("never reached"), APPROVE,
                               provider=provider)

        self.assertIn("did not take the lease label holo:writer-1", out)
        self.assertEqual(self.last_fake.turns, [])
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        self.assertEqual(provider.labels["iss-131"], ["other"])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])

    def test_a_read_back_failure_tries_the_removal_once_and_still_releases(self):
        """The board is down for the read-back and for the removal that
        follows: one removal attempt, not a retry loop, and the store
        lease still goes back -- the label left behind is this writer's
        next claim's stale one."""
        class Down(StubProvider):
            def issue_labels(self, issue_id):
                raise RuntimeError("linear timed out on the read-back")

            def unlabel_issue(self, issue_id, name):
                self.label_calls.append(("unlabel", issue_id, name))
                raise RuntimeError("linear is down")

        provider = Down(a_task())
        self.main_output(Commit("never reached"), APPROVE, provider=provider)

        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.labels["iss-131"], ["holo:writer-1"])
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        self.assertEqual(
            self.read("SELECT COUNT(*) FROM runEvents WHERE kind = 'warning'"
                      " AND summary LIKE '%could not be removed%'"), [(1,)])

    def test_a_requeue_takes_off_the_label_before_the_ticket_is_claimable(self):
        """`--requeue KO-131` removes this writer's label -- run 1's, the
        one whose close-out the board missed -- while the ticket is still
        `in_flight` and no loop can claim it, so a fresh claim's label can
        never be the one it takes off; nothing else on the issue moves."""
        ended = self.seed_ended_run(requeue=False)
        db = self.db
        status_at_removal = []

        class Watched(StubProvider):
            def unlabel_issue(self, issue_id, name):
                conn = store.open(str(db))
                try:
                    status_at_removal.append(conn.execute(
                        "SELECT status FROM tickets WHERE linearIssueId = ?",
                        (issue_id,)).fetchone()[0])
                finally:
                    conn.close()
                super().unlabel_issue(issue_id, name)

        provider = self.labelled(Watched, ["other", self.label()])
        out = io.StringIO()
        holophyte.loop.requeue(self.tgt, "KO-131", "board back", out,
                               provider=provider)

        self.assertEqual(out.getvalue().strip(),
                         f"[holo2] KO-131 requeued after run {ended}")
        self.assertEqual(status_at_removal, ["in_flight"])
        self.assertEqual(self.read("SELECT status FROM tickets"), [("ready",)])
        self.assertEqual(provider.label_calls,
                         [("unlabel", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.labels["iss-131"], ["other"])

# A scripted `WAIT` "exit" that is the timer tick instead: no child exited
# before the deadline (KO-353).
TICK = object()


class FakePool:
    """The spawn and wait seams of the scheduler, scripted.

    `SPAWN` records each command line and hands back a child with a pid of
    its own; `WAIT` takes the next scripted exit -- `(code, before)`, where
    `before` runs against the provider just before the exit is reported, the
    way a real worker's merge empties its ticket out of the board's listing
    -- and reports it for the oldest live child. Nothing here forks: the
    pids are numbers, and the test reads what would have run.
    """

    def __init__(self, exits):
        self.exits = list(exits)
        self.spawned = []   # the command lines, in spawn order
        self.envs = []
        self.alive = []     # pids, oldest first
        self.reaped = []
        self.timeouts = []  # the timeout each WAIT was called with
        self.next_pid = 5000

    def spawn(self, argv, **kwargs):
        self.spawned.append(argv)
        self.envs.append(kwargs.get("env") or {})
        self.next_pid += 1
        self.alive.append(self.next_pid)
        return type("Child", (), {"pid": self.next_pid})()

    def wait(self, children, timeout):
        if not self.alive:
            raise AssertionError("the scheduler waited with no child alive")
        if set(children) != set(self.alive):
            raise AssertionError(f"the scheduler waited on {sorted(children)}"
                                 f" with {self.alive} alive")
        self.timeouts.append(timeout)
        code, before = self.exits.pop(0)
        if before is not None:
            before()
        if code is TICK:
            # The deadline passed with no exit: the scheduler recounts.
            return None, None
        pid = self.alive.pop(0)
        self.reaped.append((pid, code))
        return pid, code


class PoolTests(LoopFixture):
    """`[loop] workers > 1`: the main process schedules a pool of
    `--worker` children sized to the claimable queue (KO-343). Spawn and
    wait go through seams, so no process is started and the test reads
    the counts."""

    def run_scheduler(self, workers, provider, exits, stop_on_failure=True,
                      tick_sec=None):
        tick = f"tick_sec = {tick_sec}\n" if tick_sec is not None else ""
        self.configure(f"[loop]\nworkers = {workers}\n"
                       f"stop_on_failure = {str(stop_on_failure).lower()}\n"
                       + tick)
        pool = FakePool(exits)
        out = io.StringIO()
        with patch.object(holophyte.loop, "SPAWN", pool.spawn), \
                patch.object(holophyte.loop, "WAIT", pool.wait), \
                patch.object(sys, "orig_argv",
                             ["python3", "-u", "factory.py", str(self.target)]), \
                patch.object(sys, "stdout", out):
            self.rc = holophyte.loop.main(self.tgt, provider)
        self.out = out.getvalue()
        return pool

    def test_the_pool_follows_the_claimable_queue_up_to_the_ceiling(self):
        """Five ready tickets under `workers = 3`: three workers at once, a
        fourth when one exits with four still claimable, and the scheduler
        exits 0 once the listing is empty and the last child is in."""
        provider = StubProvider(*(a_task(n) for n in range(1, 6)))

        def merged_one():
            provider.queue.pop(0)

        def merged_the_rest():
            provider.queue.clear()

        pool = self.run_scheduler(3, provider, [
            (holophyte.loop.WORKER_MERGED, merged_one),
            (holophyte.loop.WORKER_MERGED, merged_the_rest),
            (holophyte.loop.WORKER_MERGED, None),
            (holophyte.loop.WORKER_MERGED, None),
        ])

        # Three at the first tick, one more at the second, none after the
        # listing emptied: four spawns, every one waited for.
        self.assertEqual(len(pool.spawned), 4)
        self.assertEqual(len(pool.reaped), 4)
        self.assertEqual(pool.alive, [])
        self.assertIsNone(self.rc)
        # Each child is this command line plus `--worker`, with the slot in
        # its environment for the `[holo2 wN]` prefix.
        self.assertEqual([argv[1:] for argv in pool.spawned],
                         [["-u", "factory.py", str(self.target), "--worker"]] * 4)
        self.assertEqual([env[holophyte.loop.WORKER_SLOT_ENV]
                          for env in pool.envs], ["1", "2", "3", "4"])
        self.assertIn("[holo2] Linear has no ready tickets. done.", self.out)
        # The exit note a re-exec'd scheduler leaves for the sweep.
        self.assertEqual(self.read("SELECT COUNT(*) FROM loopRestarts"), [(0,)])

    def test_a_timer_tick_with_a_slot_free_spawns_for_a_ticket_filed_since(self):
        """`workers = 3`, one ticket and so one worker; a second ticket filed
        while it runs. The wait carries the tick as its timeout while a slot
        is free, and a wait that times out recounts the queue and spawns
        the second worker, then waits again (KO-353)."""
        provider = StubProvider(a_task(1))
        conn = holophyte.runs.open_store(self.tgt)
        self.addCleanup(conn.close)
        project = store.ensure_project(conn, provider.team, self.target)

        def filed_one():
            # Worker 1 holds ticket 1; ticket 2 arrives on the board.
            store.claim(conn, project,
                        holophyte.board.mirror_task(conn, project, a_task(1)))
            conn.commit()
            provider.queue.append(a_task(2))

        pool = self.run_scheduler(3, provider, [
            (TICK, filed_one),
            (holophyte.loop.WORKER_MERGED, provider.queue.clear),
            (holophyte.loop.WORKER_MERGED, None),
        ], tick_sec=45)

        self.assertEqual(len(pool.spawned), 2)
        self.assertEqual(len(pool.reaped), 2)
        self.assertEqual(pool.timeouts, [45, 45, 45])
        self.assertIsNone(self.rc)
        # The tick itself printed nothing; only the spawn it made shows.
        self.assertEqual(self.out.splitlines(), [
            "[holo2] started worker 1 as pid 5001",
            "[holo2] started worker 2 as pid 5002",
            "[holo2] worker 1 merged its ticket",
            "[holo2] worker 2 merged its ticket",
            "[holo2] Linear has no ready tickets. done.",
        ])

    def test_a_full_pool_waits_on_exits_alone(self):
        """Three ready tickets under `workers = 3`: the pool is full, so
        the wait carries no timeout; once one exits with the listing
        emptied, two slots are free and the timer is back (KO-353)."""
        provider = StubProvider(*(a_task(n) for n in range(1, 4)))

        pool = self.run_scheduler(3, provider, [
            (holophyte.loop.WORKER_MERGED, provider.queue.clear),
            (holophyte.loop.WORKER_MERGED, None),
            (holophyte.loop.WORKER_MERGED, None),
        ])

        self.assertEqual(len(pool.spawned), 3)
        self.assertEqual(pool.timeouts, [None, 120, 120])
        self.assertIsNone(self.rc)

    def test_the_pool_refills_while_live_workers_hold_their_leases(self):
        """Five ready tickets, `workers = 3`, and workers that really hold
        their tickets: after the first exit the two survivors each lease
        one, two tickets stay free, and the scheduler must still spawn a
        fourth -- the pool is the live workers plus the free tickets, not
        the free tickets alone (a fake spawn that never leased hid this)."""
        provider = StubProvider(*(a_task(n) for n in range(1, 6)))
        conn = holophyte.runs.open_store(self.tgt)
        self.addCleanup(conn.close)
        project = store.ensure_project(conn, provider.team, self.target)

        def first_exit():
            # Worker 1 merged ticket 1; workers 2 and 3 hold tickets 2 and 3.
            provider.queue.pop(0)
            for n in (2, 3):
                store.claim(conn, project,
                            holophyte.board.mirror_task(conn, project, a_task(n)))
            conn.commit()

        pool = self.run_scheduler(3, provider, [
            (holophyte.loop.WORKER_MERGED, first_exit),
            (holophyte.loop.WORKER_MERGED, provider.queue.clear),
            (holophyte.loop.WORKER_MERGED, None),
            (holophyte.loop.WORKER_MERGED, None),
        ])

        # Three at the first tick, a fourth at the second: two free tickets
        # and two live workers make a pool of three, one short.
        self.assertEqual(len(pool.spawned), 4)
        self.assertIsNone(self.rc)

    def test_a_leased_ticket_is_not_counted_as_claimable(self):
        """Two ready tickets, one already held by a live run on this
        target: one worker, not two."""
        provider = StubProvider(a_task(1), a_task(2))
        conn = holophyte.runs.open_store(self.tgt)
        self.addCleanup(conn.close)
        project = store.ensure_project(conn, provider.team, self.target)
        ticket = holophyte.board.mirror_task(conn, project, a_task(1))
        store.claim(conn, project, ticket)

        pool = self.run_scheduler(3, provider, [
            (holophyte.loop.WORKER_MERGED, provider.queue.clear),
        ])

        self.assertEqual(len(pool.spawned), 1)
        self.assertIsNone(self.rc)

    def test_the_claimable_count_is_one_store_read_per_tick(self):
        """Five listed tickets, three of them with a dependency: the count
        asks the store once, not once per ticket and again per dependency
        (the ticket's note holds the tick to one store read; the review
        counted seven selects for five tickets)."""
        provider = StubProvider(*(a_task(n) for n in range(1, 6)))
        conn = holophyte.runs.open_store(self.tgt)
        self.addCleanup(conn.close)
        project = store.ensure_project(conn, provider.team, self.target)
        ids = [holophyte.board.mirror_task(conn, project, a_task(n))
               for n in range(1, 6)]
        for ticket in ids[2:]:
            conn.execute("UPDATE tickets SET dependsOn = ? WHERE id = ?",
                         (json.dumps([a_task(1)["issue_id"]]), ticket))
        conn.commit()
        statements = []
        conn.set_trace_callback(statements.append)
        self.addCleanup(conn.set_trace_callback, None)

        counted = holophyte.loop._claimable(conn, project, provider.queue)

        # Two claimable: the first two; the other three wait on the first.
        self.assertEqual(counted, 2)
        self.assertEqual(len(statements), 1, statements)

    def test_a_dependency_blocked_ticket_is_not_counted_as_claimable(self):
        """Two ready tickets, the second depending on the first, which is not
        merged: one worker, not two. The count asks the store's own
        `pickable()`, dependencies included, not status and lease alone."""
        provider = StubProvider(a_task(1), a_task(2))
        conn = holophyte.runs.open_store(self.tgt)
        self.addCleanup(conn.close)
        project = store.ensure_project(conn, provider.team, self.target)
        holophyte.board.mirror_task(conn, project, a_task(1))
        second = holophyte.board.mirror_task(conn, project, a_task(2))
        conn.execute("UPDATE tickets SET dependsOn = ? WHERE id = ?",
                     (json.dumps([a_task(1)["issue_id"]]), second))
        conn.commit()

        pool = self.run_scheduler(3, provider, [
            (holophyte.loop.WORKER_MERGED, lambda: provider.queue.pop(0)),
        ])

        self.assertEqual(len(pool.spawned), 1)
        self.assertIsNone(self.rc)

    def test_a_listing_failure_is_not_an_empty_queue(self):
        """The board cannot be asked: nothing is spawned, and the scheduler
        exits nonzero rather than reporting an empty queue it never saw."""
        provider = StubProvider(a_task(1), a_task(2))

        def down():
            raise RuntimeError("linear unreachable")

        provider.ready_issues = down

        pool = self.run_scheduler(2, provider, [])

        self.assertEqual(pool.spawned, [])
        self.assertEqual(self.rc, 1)
        self.assertNotIn("Linear has no ready tickets", self.out)
        self.assertIn("linear unreachable", self.out)

    def test_stop_on_failure_drains_the_pool_and_exits_nonzero(self):
        """`workers = 2`, `stop_on_failure = true`: a worker exits failed
        while another runs. No new worker is spawned though tickets remain,
        the running one is waited for, and the exit is nonzero."""
        provider = StubProvider(*(a_task(n) for n in range(1, 5)))

        pool = self.run_scheduler(2, provider, [
            (holophyte.loop.WORKER_FAILED, None),
            (holophyte.loop.WORKER_MERGED, None),
        ])

        self.assertEqual(len(pool.spawned), 2)
        self.assertEqual([code for _, code in pool.reaped],
                         [holophyte.loop.WORKER_FAILED,
                          holophyte.loop.WORKER_MERGED])
        self.assertEqual(pool.alive, [])
        self.assertEqual(self.rc, 1)
        self.assertIn("[holo2] worker 1 failed (exit 1)", self.out)

    def test_stop_on_failure_false_keeps_spawning_and_exits_nonzero(self):
        provider = StubProvider(*(a_task(n) for n in range(1, 4)))

        pool = self.run_scheduler(2, provider, [
            (holophyte.loop.WORKER_FAILED, None),
            (holophyte.loop.WORKER_MERGED, provider.queue.clear),
            (holophyte.loop.WORKER_MERGED, None),
        ], stop_on_failure=False)

        self.assertEqual(len(pool.spawned), 3)
        self.assertEqual(self.rc, 1)

    def test_a_self_merge_re_execs_after_the_pool_drains(self):
        """A worker merged a change to the factory itself: nothing new is
        spawned, the other worker finishes on the code it started with, and
        only then does the scheduler re-exec."""
        provider = StubProvider(*(a_task(n) for n in range(1, 4)))
        execs = []
        with patch.object(holophyte.loop, "EXEC",
                          lambda *args: execs.append(args)), \
                patch.object(holophyte.loop, "__file__",
                             str(self.target / "holophyte" / "loop.py")):
            pool = self.run_scheduler(2, provider, [
                (holophyte.loop.WORKER_MERGED, None),
                (holophyte.loop.WORKER_MERGED, None),
            ])

        self.assertEqual(len(pool.spawned), 2)
        self.assertEqual(pool.alive, [])
        self.assertEqual(len(execs), 1)
        self.assertEqual(self.read("SELECT COUNT(*) FROM loopRestarts"), [(1,)])

    def test_a_failure_under_stop_on_failure_is_not_lost_to_a_self_merge(self):
        """`workers = 2`, `stop_on_failure = true`, self-hosted: one worker
        fails and the other merges a change to the factory itself. The stop
        wins: no re-exec -- a restarted scheduler would spawn again and exit
        clean -- and the exit is nonzero."""
        provider = StubProvider(*(a_task(n) for n in range(1, 5)))
        execs = []
        with patch.object(holophyte.loop, "EXEC",
                          lambda *args: execs.append(args)), \
                patch.object(holophyte.loop, "__file__",
                             str(self.target / "holophyte" / "loop.py")):
            pool = self.run_scheduler(2, provider, [
                (holophyte.loop.WORKER_FAILED, None),
                (holophyte.loop.WORKER_MERGED, None),
            ])

        self.assertEqual(len(pool.spawned), 2)
        self.assertEqual(pool.alive, [])
        self.assertEqual(execs, [])
        self.assertEqual(self.rc, 1)
        self.assertEqual(self.read("SELECT COUNT(*) FROM loopRestarts"), [(0,)])

    def test_every_spawned_worker_is_reported_by_wait(self):
        """Real children through the real seams: a worker that exited before
        the next spawn is still reported by `WAIT`, with its own exit code.
        (The reviewer's reproduction: with only the pid kept, `Popen`'s
        housekeeping reaped the first child and the second wait raised.)"""
        children = {}  # pid -> Popen, as the scheduler hands them to WAIT
        script = ("import os, sys;"
                  f" sys.exit(int(os.environ['{holophyte.loop.WORKER_SLOT_ENV}']))")
        with patch.object(sys, "orig_argv", [sys.executable, "-c", script]), \
                patch.object(sys, "stdout", io.StringIO()):
            first = holophyte.loop._spawn_worker(self.tgt, 1)
            children[first.pid] = first
            # Exited but unreaped when the second is spawned.
            os.waitid(os.P_PID, first.pid, os.WEXITED | os.WNOWAIT)
            second = holophyte.loop._spawn_worker(self.tgt, 2)
            children[second.pid] = second
            reaped = {}
            for _ in range(2):
                pid, code = holophyte.loop.WAIT(children, None)
                reaped[children.pop(pid)] = code

        self.assertEqual(reaped, {first: 1, second: 2})
        self.assertEqual((first.returncode, second.returncode), (1, 2))


class WorkerTests(LoopFixture):
    """`--worker`: the serial loop's phases once, for one ticket."""

    def worker(self, *script, provider):
        fake = FakeAgent(*script)
        out = io.StringIO()
        with no_agent_processes(), \
                patch.dict(sys.modules, {"linear_provider": provider}), \
                patch.object(holophyte.loop, "agent", fake), \
                patch.dict(os.environ, {holophyte.loop.WORKER_SLOT_ENV: "2"}), \
                patch.object(sys, "stdout", out):
            rc = holophyte.loop.worker(self.tgt, provider)
        return rc, out.getvalue()

    def test_a_worker_merges_one_ticket_and_exits_merged(self):
        provider = StubProvider(a_task(1), a_task(2))

        rc, out = self.worker(Commit("the scripted work"), APPROVE,
                              provider=provider)

        self.assertEqual(rc, holophyte.loop.WORKER_MERGED)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        # The second ticket is left for another worker.
        self.assertEqual([t["id"] for t in provider.queue], ["KO-132"])
        self.assertIn("Merge task/ko-131-add-a-thing: add a thing",
                      self.subjects())
        # Its lines carry the slot, in place of the bare tag.
        self.assertIn("[holo2 w2] ", out)
        self.assertNotIn("\n[holo2] ", "\n" + out)

    def test_a_worker_prefixes_its_stderr_too(self):
        """A worker that dies before its first line -- here the store will
        not open -- writes its traceback to stderr, the stream the pool
        shares with its siblings; every line of it carries the slot, or
        the log cannot say which worker died."""
        provider = StubProvider(a_task(1))
        err = io.StringIO()
        with patch.dict(os.environ, {holophyte.loop.WORKER_SLOT_ENV: "2"}), \
                patch.object(holophyte.loop, "open_store",
                             side_effect=RuntimeError("store locked")), \
                patch.object(sys, "stdout", io.StringIO()), \
                patch.object(sys, "stderr", err):
            try:
                holophyte.loop.worker(self.tgt, provider)
            except RuntimeError:
                # What the interpreter does with an exception that reaches
                # the top of a `--worker` process.
                sys.excepthook(*sys.exc_info())
            else:
                self.fail("the worker swallowed its store failure")
        lines = err.getvalue().splitlines()
        self.assertIn("RuntimeError: store locked", "\n".join(lines))
        self.assertTrue(all(line.startswith("[holo2 w2] ") for line in lines),
                        lines)

    def test_the_prefix_survives_indentation_written_on_its_own(self):
        """Some interpreters' `traceback` writes a source line's indentation
        and the line as two writes; the prefix must still open the line, or
        a traceback's source lines lose their slot in the shared log."""
        out = io.StringIO()
        prefixed = holophyte.loop._PrefixedOut(out, "[holo2 w2]")
        prefixed.write("    ")
        prefixed.write("raise RuntimeError\n")
        prefixed.write("[holo2] run failed\n")
        self.assertEqual(out.getvalue().splitlines(),
                         ["[holo2 w2]     raise RuntimeError",
                          "[holo2 w2] run failed"])

    def test_a_worker_commits_findings_under_the_merge_lock(self):
        """The FINDINGS.md regeneration and commit run while this worker
        holds the merge lock, so no sibling is merging in the checkout while
        the file is written and `git commit` runs."""
        self.configure('[report]\nfindings = "repo"\n')
        provider = StubProvider(a_task(1))
        lock = holophyte.gates.merge_lock_path(self.tgt)
        held = []
        real = holophyte.loop.commit_findings

        def commit_under_lock(target, message):
            held.append(lock.exists())
            return real(target, message)

        with patch.object(holophyte.loop, "commit_findings", commit_under_lock):
            rc, _ = self.worker(Commit("the scripted work"), APPROVE,
                                provider=provider)

        self.assertEqual(rc, holophyte.loop.WORKER_MERGED)
        # Two commits in the run -- the gate's pre-merge one and the
        # close-out -- and the lock was held for each.
        self.assertEqual(held, [True, True])
        self.assertIn("Complete task KO-131: add a thing", self.subjects())
        self.assertFalse(lock.exists())  # and released after

    def test_a_failed_worker_renders_findings_under_the_merge_lock(self):
        """A failed run's close-out regenerates FINDINGS.md too, and a
        worker's does so under the merge lock: a sibling may be merging in
        the checkout at that moment (the review of KO-343)."""
        self.configure('[report]\nfindings = "repo"\n')
        provider = StubProvider(a_task(1))
        lock = holophyte.gates.merge_lock_path(self.tgt)
        held = []
        real = holophyte.findings.refresh_findings

        def render_under_lock(target, conn):
            held.append(lock.exists())
            return real(target, conn)

        with patch.object(holophyte.loop, "refresh_findings", render_under_lock), \
                patch.object(holophyte.board, "refresh_findings", render_under_lock):
            rc, _ = self.worker(Refuse(), provider=provider)

        self.assertEqual(rc, holophyte.loop.WORKER_FAILED)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        # Rendered once, with the lock held, and released after.
        self.assertEqual(held, [True])
        self.assertIn("KO-131", (self.tgt.path / "FINDINGS.md").read_text())
        self.assertFalse(lock.exists())

    def test_a_worker_with_nothing_to_claim_exits_idle(self):
        rc, out = self.worker(provider=StubProvider())

        self.assertEqual(rc, holophyte.loop.WORKER_IDLE)
        self.assertEqual(self.read("SELECT COUNT(*) FROM runs"), [(0,)])


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


class SweptHeartbeatTests(unittest.TestCase):
    """`heartbeat_while()` notices the run it beats for being ended (KO-339).

    The block is the loop's wait on an agent; a second connection ends the
    run mid-block the way `act_on_trip()` does. The callback -- the loop's
    kill of the turn -- fires once, and the block's exit raises `RunSwept`
    naming the run and the reason the store recorded.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"
        self.conn = store.open(self.path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = store.ensure_project(self.conn, "team_abc", "/repos/x")
        ticket = store.mirror_ticket(
            self.conn, project, "iss_1", "KO-1", "ticket one",
            acceptance_criteria=["it works"], verification_commands=["true"])
        self.run = store.claim(self.conn, project, ticket)

    def test_a_run_ended_mid_block_fires_the_callback_once_and_raises(self):
        reason = "swept by the supervisor in phase working: time_box (99 min)"
        calls = []
        fired = threading.Event()

        def on_swept():
            calls.append(time.monotonic())
            fired.set()

        with self.assertRaises(holophyte.runs.RunSwept) as caught:
            with holophyte.loop.heartbeat_while(self.conn, self.run, 0.05,
                                                on_swept=on_swept):
                other = store.open(self.path)
                try:
                    store.release(other, self.run, "failed", reason)
                finally:
                    other.close()
                self.assertTrue(fired.wait(5), "the beat never saw the end")
                # The block goes on past several more beat intervals: a
                # callback fired per beat would show up here as a second call.
                time.sleep(0.3)

        self.assertEqual(len(calls), 1)
        self.assertEqual(caught.exception.run_id, self.run)
        self.assertEqual(caught.exception.reason, reason)
        self.assertIn(f"run {self.run}", str(caught.exception))

    def test_a_run_ended_after_the_last_beat_still_raises_at_exit(self):
        # The sweep lands after the timer's last beat and the block returns
        # at once: no beat sees the end. The exit has to look for itself,
        # or the loop verifies and records against a run the store failed.
        reason = "swept by the supervisor in phase working: time_box (99 min)"
        calls = []
        with self.assertRaises(holophyte.runs.RunSwept) as caught:
            with holophyte.loop.heartbeat_while(self.conn, self.run, 60,
                                                on_swept=calls.append):
                other = store.open(self.path)
                try:
                    store.release(other, self.run, "failed", reason)
                finally:
                    other.close()
        self.assertEqual(calls, [])  # nothing left to stop: the body returned
        self.assertEqual(caught.exception.run_id, self.run)
        self.assertEqual(caught.exception.reason, reason)

    def test_a_live_run_raises_nothing_and_calls_nothing(self):
        calls = []
        with holophyte.loop.heartbeat_while(self.conn, self.run, 0.05,
                                            on_swept=calls.append):
            time.sleep(0.2)
        self.assertEqual(calls, [])


class SweptTurnTests(LoopFixture):
    """A loop whose run the supervisor swept stops that run's turn (KO-339).

    Run 160 was ended by the supervisor on its time box while the loop was
    inside a fix turn; the loop kept the agent working for twenty more
    minutes and would have verified, recorded and merged against a run the
    store had already failed. Here the implementer turn really starts a
    process in a session of its own and hands the loop its handle, as
    `agent()` does, then sweeps the store with `act` the way the supervisor
    would and waits on the process. The kill is the only way that wait ends.
    """

    def test_a_swept_run_kills_its_turn_writes_nothing_more_and_moves_on(self):
        # 0.01 min is 600 ms of stale threshold, so the loop beats every
        # 300 ms and notices the end within one beat.
        self.configure("[supervisor]\nheartbeat_stale_min = 0.01\n")
        knobs = holophyte.config.sweep_config(self.tgt)
        db, tgt = self.db, self.tgt
        seen = {}
        fake = FakeAgent()

        class BlockUntilKilled(Idle):
            """An implementer that sweeps its own run from outside, then
            blocks in a process only a kill of its group can end."""

            def play(self, cwd, turn):
                proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
                fake.turns[-1].on_start(proc)
                conn = store.open(str(db))
                try:
                    (run_id,) = conn.execute("SELECT id FROM runs").fetchone()
                    # An hour on: the run is over its 5 min time box, and
                    # one stale sighting is short of a stale trip.
                    result = holophyte.supervisor.sweep(
                        tgt, conn, int(time.time() * 1000) + 3_600_000,
                        act=True, knobs=knobs)
                    seen["trips"] = [t.condition for t in result.trips]
                    seen["row"] = conn.execute(
                        "SELECT outcome, outcomeReason, endedAt FROM runs"
                        " WHERE id = ?", (run_id,)).fetchone()
                    seen["events"] = conn.execute(
                        "SELECT COUNT(*) FROM runEvents WHERE runId = ?",
                        (run_id,)).fetchone()[0]
                    seen["rounds"] = conn.execute(
                        "SELECT COUNT(*) FROM reviewRounds WHERE runId = ?",
                        (run_id,)).fetchone()[0]
                finally:
                    conn.close()
                try:
                    proc.wait(timeout=20)
                    seen["returncode"] = proc.returncode
                except subprocess.TimeoutExpired:
                    # The loop never killed it: end it here so the test
                    # fails on the record below rather than hanging.
                    proc.kill()
                    proc.wait()
                    seen["returncode"] = "the loop never killed the turn"
                return "killed"

        fake.script = [BlockUntilKilled(), Commit("the next work"), APPROVE]
        provider = StubProvider(a_task(1), a_task(2))
        out = io.StringIO()
        with patch.object(sys, "stdout", out):
            self.loop(provider=provider, fake=fake)
        out = out.getvalue()

        # The sweep tripped the time box and ended the run.
        self.assertEqual(seen["trips"], ["time_box"])
        self.assertEqual(seen["row"][0], "failed")
        self.assertIn("swept by the supervisor", seen["row"][1])
        self.assertIsNotNone(seen["row"][2])
        # The turn's process was killed, not left to finish its 30 seconds.
        self.assertEqual(seen["returncode"], -signal.SIGKILL)
        self.assertIn("[holo2] run 1 was ended by the supervisor"
                      f" ({seen['row'][1]}); stopping this turn", out)
        # Nothing more was written to the swept run: its row and its
        # streams are as the sweep left them.
        self.assertEqual(
            self.read("SELECT outcome, outcomeReason, endedAt FROM runs"
                      " WHERE id = 1"), [seen["row"]])
        self.assertEqual(
            self.read("SELECT COUNT(*) FROM runEvents WHERE runId = 1"),
            [(seen["events"],)])
        self.assertEqual(
            self.read("SELECT COUNT(*) FROM reviewRounds WHERE runId = 1"),
            [(seen["rounds"],)])
        # Worktree and branch are as the sweep preserved them.
        self.assertIn("task/ko-131-add-a-thing", self.branches())
        self.assertTrue(any(p.is_dir() for p in self.worktrees.iterdir()))
        # The loop made its next claim and finished that run on its own.
        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        self.assertIn(("iss-132", "In Progress"), provider.states)
        self.assertEqual(self.read("SELECT id, outcome FROM runs ORDER BY id"),
                         [(1, "failed"), (2, "merged")])
        self.assertIn("the next work", self.subjects())
        self.assertIsNone(self.rc)


class ImplementerProbeTests(LoopFixture):
    """A configured `[agents] implementer` is asked for one word before the
    pass claims anything (KO-357). The route under test is a real script the
    loop really runs -- the fake answers turns, not the probe -- so a pass or
    a refusal here is the process boundary's word, not the patch's."""

    def script(self, body):
        path = self.db.parent / "implementer.sh"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        self.configure(f'[agents]\nimplementer = "{path}"\n')
        return path

    def test_a_route_that_answers_ready_lets_the_pass_proceed(self):
        path = self.script('echo "ready"\n')
        out = self.main_output(Commit("work"), APPROVE)
        self.assertIn("implementer probe passed", out)
        self.assertIn(str(path), out)
        self.assertTrue(any(subject.startswith("Merge task/")
                            for subject in self.subjects()), self.subjects())
        self.assertIsNone(self.rc)

    def test_a_route_that_exits_nonzero_ends_the_pass_before_any_claim(self):
        path = self.script("echo broken harness >&2\nexit 1\n")
        out = self.main_output(Commit("work"), APPROVE)
        self.assertEqual(self.rc, 1)
        self.assertIn("implementer probe failed (exit 1)", out)
        self.assertIn(str(path), out)
        self.assertIn("broken harness", out)
        # Nothing was claimed: the store was never opened, so no run row
        # exists; the board saw no transition; the scripted implementer was
        # never asked for a turn.
        self.assertFalse(self.db.exists())
        self.assertEqual(self.last_provider.states, [])
        self.assertEqual(len(self.last_provider.queue), 1)
        self.assertEqual(self.last_fake.turns, [])

    def test_a_route_that_cannot_start_ends_the_pass_naming_the_reason(self):
        """An absolute path that does not exist passes the config check and
        fails only at launch; that is a failed probe naming the OS's reason,
        not a traceback, and nothing is claimed."""
        missing = self.db.parent / "no-such-harness"
        self.configure(f'[agents]\nimplementer = "{missing}"\n')
        out = self.main_output(Commit("work"), APPROVE)
        self.assertEqual(self.rc, 1)
        self.assertIn("implementer probe failed (could not start:", out)
        self.assertIn("No such file", out)
        self.assertIn(str(missing), out)
        self.assertFalse(self.db.exists())

    def test_a_route_that_hangs_past_the_cap_ends_the_pass_naming_it(self):
        path = self.script("sleep 30\n")
        with patch.object(holophyte.agents, "PROBE_TIMEOUT", 1):
            out = self.main_output(Commit("work"), APPROVE)
        self.assertEqual(self.rc, 1)
        self.assertIn("implementer probe failed (no answer within 1s)", out)
        self.assertIn(str(path), out)
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
