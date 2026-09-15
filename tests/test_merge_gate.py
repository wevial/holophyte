"""The merge gate and the run's close-out, driven end to end with zero agent calls.

What happens at and after the gate: the approval park and its resume
(`_park_for_approval`, `_resume_at_merge_gate`), `[merge] after`, and the
self-hosted re-exec after a merge; plus the run's close-out wrapper (crash
containment and its reason, stop-on-failure, `[report] findings`) and the
run's liveness under the store (the heartbeat, a store-ended run). The
harness is `tests/loop_fixture.py`: a real throwaway repo, a real store, a
stub provider, and `tests/fake_agent.py` scripting the agent turns.

Run: python3 -m unittest discover -s tests -p 'test_merge_gate*' -v
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
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
    FAIL,
    REQUEST_CHANGES,
    Commit,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    Boom,
    InfraRefuse,
    Interrupt,
    LoopFixture,
    Refuse,
    StubProvider,
    a_task,
)

import holophyte.board  # noqa: E402 - after the sys.path insert above
import holophyte.config_tables  # noqa: E402 - after the sys.path insert above
import holophyte.findings  # noqa: E402 - after the sys.path insert above
import holophyte.loop  # noqa: E402 - after the sys.path insert above
import holophyte.merge_gate  # noqa: E402 - after the sys.path insert above
import holophyte.operator  # noqa: E402 - after the sys.path insert above
import holophyte.runs  # noqa: E402 - after the sys.path insert above
import holophyte.serve  # noqa: E402 - after the sys.path insert above
import holophyte.supervisor  # noqa: E402 - after the sys.path insert above
import holophyte.target  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets as tickets  # noqa: E402 - after the sys.path insert above


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
