"""The loop's pass before a claim, driven end to end with zero agent calls."""
from __future__ import annotations

import io
import subprocess
import sys
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
from fake_agent import APPROVE, Commit  # noqa: E402 - after the sys.path insert above
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    INVALID_BODY,
    VALID_BODY,
    Boom,
    LoopFixture,
    MergeModeFixture,
    StubProvider,
    a_task,
)

import holophyte.board  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets as tickets  # noqa: E402 - after the sys.path insert above


class SweepDiagnosticsTests(LoopFixture):
    """A held ticket and the startup preamble surface the read-only sweep."""

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
    """Walk mirrored tickets closed on the board to their terminal status."""

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

    def abandoned_tree(self, pushed=True):
        runs = self.seed()
        branch = "holo/ko-2"
        wt = self.worktrees / "ko-2"
        remote = self.target.parent / "remote.git"
        self.git("init", "--bare", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("worktree", "add", "-b", branch, str(wt))
        (wt / "committed.txt").write_text("ticket work\n")
        self.git("add", ".", cwd=wt)
        self.git("commit", "-m", "ticket work", cwd=wt)
        if pushed:
            self.git("push", "origin", branch)
        conn = store.open(str(self.db))
        conn.execute("UPDATE runs SET branch = ? WHERE id = ?",
                     (branch, runs["KO-2"]))
        conn.commit()
        conn.close()
        return wt, branch, runs["KO-2"]

    def cancel_tree(self):
        provider = StubProvider()
        provider.closed = {"KO-2": "canceled"}
        return self.main_output(provider=provider)

    def test_abandoned_pushed_tree_is_removed_but_branch_remains(self):
        wt, branch, _ = self.abandoned_tree()
        self.cancel_tree()
        self.assertEqual(self.statuses()["KO-2"], "abandoned")
        self.assertFalse(wt.exists())
        self.assertNotIn(str(wt), self.git("worktree", "list"))
        self.git("show-ref", "--verify", f"refs/heads/{branch}")

    def test_abandoned_tree_reachable_from_main_needs_no_remote(self):
        wt, branch, _ = self.abandoned_tree(pushed=False)
        self.git("merge", "--ff-only", branch)
        self.git("remote", "remove", "origin")
        self.cancel_tree()
        self.assertFalse(wt.exists())
        self.git("show-ref", "--verify", f"refs/heads/{branch}")

    def test_stale_remote_tracking_ref_does_not_allow_retirement(self):
        wt, branch, _ = self.abandoned_tree()
        remote = self.target.parent / "remote.git"
        self.git("update-ref", "-d", f"refs/heads/{branch}", cwd=remote)
        self.git("show-ref", "--verify", f"refs/remotes/origin/{branch}")
        self.assertIn("commits exist nowhere else", self.cancel_tree())
        self.assertTrue(wt.is_dir())

    def test_remote_verification_failures_are_preserved_and_reported(self):
        wt, _, run_id = self.abandoned_tree()
        real_run = subprocess.run
        for command in ("ls-remote", "fetch"):
            with self.subTest(command=command):
                def fail_remote(args, **kwargs):
                    if args[:2] == ["git", command]:
                        return subprocess.CompletedProcess(
                            args, 128, stdout="", stderr="fatal: transport unavailable")
                    return real_run(args, **kwargs)
                with patch("holophyte.claim.subprocess.run", side_effect=fail_remote):
                    printed = self.cancel_tree()
                self.assertTrue(wt.is_dir())
                line = next(line for line in printed.splitlines()
                            if "remote verification failed" in line)
                self.assertIn(command, line)
                self.assertIn("transport unavailable", line)
                self.assertIn("KO-2", line)
                self.assertNotIn("commits exist nowhere else", printed)
                self.assertIn((run_id, line), self.read(
                    "SELECT runId, summary FROM runEvents"))
                # Let the next subcase witness the same cancellation transition.
                conn = store.open(str(self.db))
                conn.execute("UPDATE tickets SET status = 'ready'"
                             " WHERE linearIdentifier = 'KO-2'")
                conn.commit()
                conn.close()

    def test_abandoned_dirty_tree_is_preserved_and_reported(self):
        wt, _, run_id = self.abandoned_tree()
        (wt / "uncommitted.txt").write_bytes(b"preserve these bytes\n")
        printed = self.cancel_tree()
        self.assertEqual((wt / "uncommitted.txt").read_bytes(),
                         b"preserve these bytes\n")
        line = next(line for line in printed.splitlines()
                    if "uncommitted work" in line)
        self.assertIn("KO-2", line)
        self.assertIn((run_id, line), self.read(
            "SELECT runId, summary FROM runEvents"))

    def test_abandoned_unpushed_commits_are_preserved(self):
        wt, _, run_id = self.abandoned_tree(pushed=False)
        printed = self.cancel_tree()
        self.assertTrue(wt.is_dir())
        line = next(line for line in printed.splitlines()
                    if "commits exist nowhere else" in line)
        self.assertIn("KO-2", line)
        self.assertIn((run_id, line), self.read(
            "SELECT runId, summary FROM runEvents"))

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

        # KO-3's ready row then met the empty pass's reconcile (KO-425).
        self.assertEqual(self.statuses(), {"KO-1": "merged", "KO-2": "abandoned",
                                          "KO-3": "blocked_on_deps"})
        self.assertEqual(self.reconcile_rows(),
                         [(runs["KO-1"], "supervisor", "linear_completed"),
                          (runs["KO-2"], "supervisor", "linear_cancelled")])
        self.assertIn("[holo2] reconciled KO-1: ready -> merged"
                      " (Linear completed)", printed)
        self.assertIn("[holo2] reconciled KO-2: needs_spec -> abandoned"
                      " (Linear canceled)", printed)
        self.assertNotIn("reconciled KO-3", printed)
        self.assertIn("waiting on the board: KO-3", printed)
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

        # KO-3's ready row waits on the board per the reconcile (KO-425).
        self.assertEqual(self.statuses(), {"KO-1": "in_flight", "KO-2": "abandoned",
                                          "KO-3": "blocked_on_deps"})
        self.assertEqual(
            self.read("SELECT phase, endedAt FROM runs WHERE id = %d"
                      % provider.run_id), [("claimed", None)])
        self.assertEqual([row[2] for row in self.reconcile_rows()],
                         ["linear_cancelled"])
        self.assertNotIn("reconciled KO-1", printed)
        self.assertIn("reconcile left KO-1 alone", printed)

    def test_only_this_projects_tickets_are_reconciled(self):
        """The provider knows one team; another project's open tickets are
        that project's loop to reconcile."""
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
        # KO-425: the empty pass then parked the unlisted `ready` rows.
        self.assertEqual(self.statuses(),
                         {"KO-1": "blocked_on_deps", "KO-2": "needs_spec",
                          "KO-3": "blocked_on_deps", "KO-131": "merged"})
        self.assertIn("the scripted work", self.subjects())


class QueueMirrorTests(LoopFixture):
    """Each claim mirrors every ready issue the provider lists, so the Board
    shows the queue and not only the ticket the loop picked."""

    def statuses(self):
        return dict(self.read("SELECT linearIdentifier, status FROM tickets"))

    def queue(self):
        """Claim a valid ticket after skipping invalid contracts."""
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


class RejectedPullRequestTests(MergeModeFixture):
    def test_closed_parked_pr_is_rejected_without_a_strike(self):
        import test_pullrequest
        helpers = test_pullrequest.MergeModePullRequestTests
        helpers.parked_on_pr(self)
        branch, sha = self.read("SELECT branch, candidateSha FROM runs")[0]
        conn = store.open(self.db)
        self.addCleanup(conn.close)
        ticket = conn.execute("SELECT id FROM tickets").fetchone()[0]
        before = store.read.failed_attempts_since(conn, ticket, 0)
        helpers.fake_client(self, dict(helpers.CLOSED_PULL, timelineItems={
            "nodes": [{"actor": {"login": "alice"}}]}))
        provider = StubProvider()
        label = holophyte.board.lease_label(self.tgt)
        provider.labels["iss-131"] = [label]
        provider.closed = {"KO-131": "canceled"}
        self.main_output(provider=provider)
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("rejected", "rejected")])
        reason = self.read("SELECT outcomeReason FROM runs")[0][0]
        self.assertIn("alice", reason)
        self.assertIn(sha, reason)
        self.assertEqual(self.read("SELECT status, blockedQuestion FROM tickets"),
                         [("blocked_on_operator",
                           f"rejected: {self.URL} closed by alice")])
        self.assertNotIn(label, provider.labels["iss-131"])
        self.assertIn(("unlabel", "iss-131", label), provider.label_calls)
        self.assertEqual(provider.states, [])
        self.assertEqual(store.read.failed_attempts_since(conn, ticket, 0), before)
        self.assertEqual(self.git("rev-parse", branch).strip(), sha)
