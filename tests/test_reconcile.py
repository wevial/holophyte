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
import holophyte.reconcile  # noqa: E402 - after the sys.path insert above
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
        label = holophyte.board.lease_label(self.project)
        provider.labels["iss-131"] = [label]
        provider.closed = {"KO-131": "canceled"}
        self.main_output(provider=provider)
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("rejected", "rejected")])
        self.assertEqual(self.read("SELECT parkKind FROM runs"),
                         [("pull_request_closed",)])
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


class FailedRunPullRequestTests(MergeModeFixture):
    """KO-722: a run that failed in a pull-request project leaves its pull
    request open and the ticket `in_flight`; a person merging it later is
    the landing `--close` would record."""

    def failed_with_pr(self, pull):
        from test_pullrequest import MergeModePullRequestTests as H
        H.parked_on_pr(self)
        conn = store.open(self.db)
        self.addCleanup(conn.close)
        # What REL-138 run 64 left: the run ended failed with its pull
        # request URL kept, the ticket in flight with no live run.
        with conn:
            conn.execute("UPDATE runs SET phase = 'failed', outcome = 'failed',"
                         " outcomeReason = 'babysit rounds exhausted',"
                         " endedAt = COALESCE(endedAt, lastHeartbeat)")
            conn.execute("UPDATE tickets SET status = 'in_flight',"
                         " blockedQuestion = NULL")
        H.fake_client(self, pull)
        provider = StubProvider()
        label = holophyte.board.lease_label(self.project)
        provider.labels["iss-131"] = [label]
        before = self.read("SELECT id FROM interventions")
        out = io.StringIO()
        with patch.object(sys, "stdout", out):
            holophyte.reconcile._reconcile_pull_requests(
                self.project, conn,
                conn.execute("SELECT id FROM projects").fetchone()[0], provider)
        return provider, before, out.getvalue()

    def test_merged_pull_request_closes_the_ticket_out(self):
        from test_pullrequest import MergeModePullRequestTests as H
        merged = dict(H.MERGED_PULL, mergedBy={"login": "maintainer"})
        provider, before, out = self.failed_with_pr(merged)
        self.assertEqual(self.read("SELECT status, blockedQuestion FROM tickets"),
                         [("merged", None)])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("failed", None)])
        last = max((i for (i,) in before), default=0)
        self.assertEqual(self.read("SELECT runId, action FROM interventions"
                                   f" WHERE id > {last}"), [(1, "close_out")])
        ((note,),) = self.read("SELECT summary FROM runEvents WHERE summary"
                               " LIKE 'human close_out:%'")
        self.assertIn(self.URL, note)
        self.assertIn(self.MERGE_SHA[:12], note)
        self.assertIn("maintainer", note)
        self.assertIn(("iss-131", "Done"), provider.states)
        self.assertEqual(len(provider.comments), 1)
        self.assertIn("KO-131", out)

    def assert_unchanged(self, pull):
        provider, before, _ = self.failed_with_pr(pull)
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("in_flight",)])
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("failed", "failed")])
        self.assertEqual(self.read("SELECT id FROM interventions"), before)
        self.assertEqual(provider.states, [])

    def test_open_pull_request_changes_nothing(self):
        from test_pullrequest import MergeModePullRequestTests as H
        self.assert_unchanged(H.OPEN_PULL)

    def test_pull_request_closed_unmerged_changes_nothing(self):
        from test_pullrequest import MergeModePullRequestTests as H
        self.assert_unchanged(H.CLOSED_PULL)


class ContentWakeTests(MergeModeFixture):
    def parked_on_pr(self):
        from test_pullrequest import MergeModePullRequestTests as H
        H.parked_on_pr(self)

    def test_timestamp_only_bump_refreshes_facts_without_intervention(self):
        from test_pullrequest import MergeModePullRequestTests as H
        H.parked_with_mark(self, H.T1, 3)
        old = {'id': 'old', 'createdAt': H.T1, 'submittedAt': H.T1,
               'author': {'login': 'person'}, 'body': 'Already seen'}
        node = dict(H.OPEN_PULL, updatedAt='2026-09-10T10:00:10Z',
                    comments={'nodes': [old]}, reviews={'nodes': [old]},
                    reviewThreads={'totalCount': 3, 'nodes': [
                        {'comments': {'nodes': [old]}}]},
                    commits={'nodes': [{'commit': {'oid': 'old-commit',
                        'committedDate': H.T1, 'statusCheckRollup':
                        {'state': 'SUCCESS'}}}]}, reviewDecision='APPROVED')
        from holophyte import pr_status, reconcile
        conn = store.open(self.db)
        self.addCleanup(conn.close)
        with patch.object(pr_status, 'graphql', return_value={
                'repository': {'pullRequest': dict(node, updatedAt=H.T1)}}):
            reconcile._pr_seen(self.project, pr_status.parse_pr_url(self.URL),
                               conn, store.read.blocked_tickets(conn)[0].runId)
        H.fake_client(self, node)
        self.main_output(provider=StubProvider())
        self.assertEqual(self.read("SELECT action FROM interventions "
                                   "WHERE action = 'babysit'"), [])
        self.assertEqual(self.read("SELECT phase, prSeenAt, prSeenThreads, "
                                   "prSeenChecks, prSeenReview FROM runs"),
                         [('awaiting_merge_approval', H.T1, 3,
                           'success', 'approved')])

    def test_only_new_authored_content_wakes_once(self):
        from test_pullrequest import MergeModePullRequestTests as H
        H.parked_with_mark(self, H.T1, 3)
        cases = [({}, False),
                 ({'comments': {'nodes': [{'id': 'own', 'createdAt': H.T2,
                    'author': {'login': 'factory'},
                    'body': '---- Comment by implementer ----\nreply'}]}}, False),
                 ({'comments': {'nodes': [{'id': 'bot', 'createdAt': H.T1,
                    'updatedAt': H.T2, 'author': {'login': 'bot',
                    '__typename': 'Bot'}, 'body': 'edited'}]}}, False)]
        for field, label in [('comments', 'conversation comment'),
                             ('reviews', 'review'), ('commits', 'commit'),
                             ('reviewThreads', 'inline thread')]:
            item = {'id': label, 'createdAt': H.T2, 'submittedAt': H.T2,
                    'committedDate': H.T2, 'oid': 'other-sha',
                    'author': {'login': 'factory', 'user': {'login': 'person'}},
                    'body': 'Please check this'}
            if field == 'commits':
                item = {'commit': item}
            if field == 'reviewThreads':
                item = {'comments': {'nodes': [item]}}
            cases.append(({field: {'nodes': [item]}}, label))
        conn = store.open(self.db)
        self.addCleanup(conn.close)
        import holophyte.pr_status as ps
        from holophyte.reconcile import _rebabysit
        ticket = store.read.blocked_tickets(conn)[0]
        pull = ps.parse_pr_url(self.URL)
        for content, expected in cases:
            with self.subTest(content=content):
                node = dict(H.OPEN_PULL, updatedAt=H.T2,
                            reviewThreads={'totalCount': 3})
                node.update(content)
                with patch.object(ps, 'graphql', return_value={
                        'viewer': {'login': 'factory'},
                        'repository': {'pullRequest': node}}):
                    status = ps.pull_status(self.project, pull)
                with patch.object(store, 'babysit') as wake:
                    _rebabysit(conn, ticket, pull, status, 0)
                    if expected:
                        wake.assert_called_once()
                        self.assertIn(expected, wake.call_args.args[2])
                        wake.reset_mock()
                        _rebabysit(conn, ticket, pull, status, 0)
                    wake.assert_not_called()
                conn.execute('UPDATE runs SET prSeenAt = ?', (H.T1,))
                conn.commit()

    def test_two_empty_passes_raise_attention_until_real_content_arrives(self):
        from test_pullrequest import MergeModePullRequestTests as H

        from holophyte.serve import parked_item
        H.parked_with_mark(self, H.T1, 0)
        conn = store.open(self.db)
        self.addCleanup(conn.close)
        for n, at in enumerate((H.T2, H.T3), 1):
            with self.subTest(pass_number=n):
                active = dict(H.OPEN_PULL, updatedAt=at, comments={'nodes': [
                    {'id': f'comment-{n}', 'createdAt': at,
                     'author': {'login': 'person'}, 'body': 'Please check'}]})
                quiet = dict(H.OPEN_PULL, updatedAt=at)
                H.fake_client(self, active, quiet)
                self.main_output(provider=self.provider())
                self.assertEqual(self.read("SELECT summary FROM runEvents WHERE "
                                          "kind = 'pr_empty_wakes' ORDER BY id")[-1],
                                 (str(n),))
                conn.execute('UPDATE runs SET lastHeartbeat = lastHeartbeat - 200000')
                conn.commit()
        ticket = store.read.blocked_tickets(conn)[0]
        self.assertEqual(ticket.blockedQuestion, 'woken repeatedly with nothing new')
        item = parked_item(ticket)
        self.assertEqual(item['ticket'], 'KO-131')
        self.assertEqual(item['kind'], 'pr_open')
        self.assertEqual(item['reason'], 'woken repeatedly with nothing new')
        before = self.read("SELECT id FROM interventions WHERE action = 'babysit'")
        self.main_output(provider=StubProvider())
        self.assertEqual(self.read("SELECT id FROM interventions "
                                   "WHERE action = 'babysit'"),
                         before)
        later = dict(H.OPEN_PULL, updatedAt='2026-09-10T12:00:00Z',
                     comments={'nodes': [{'id': 'real',
                     'createdAt': '2026-09-10T12:00:00Z',
                     'author': {'login': 'person'}, 'body': 'Actual new feedback'}]})
        H.fake_client(self, later)
        self.main_output(provider=self.provider())
        self.assertEqual(len(self.read("SELECT id FROM interventions "
                                       "WHERE action = 'babysit'")), len(before) + 1)
        self.assertEqual(self.read("SELECT summary FROM runEvents WHERE "
                                   "kind = 'pr_empty_wakes' ORDER BY id")[-1], ('0',))

    def test_newly_pushed_old_commit_wakes_once_after_a_real_park_read(self):
        from test_pullrequest import MergeModePullRequestTests as H

        from holophyte import pr_status, reconcile
        H.parked_with_mark(self, H.T1, 0)
        conn = store.open(self.db)
        self.addCleanup(conn.close)
        ticket = store.read.blocked_tickets(conn)[0]
        pull = pr_status.parse_pr_url(self.URL)
        old = {'commit': {'oid': 'seen', 'committedDate': H.T1,
                         'author': {'user': {'login': 'person'}}}}
        node = dict(H.OPEN_PULL, updatedAt=H.T1, commits={'nodes': [old]})
        answer = {'repository': {'pullRequest': node},
                  'viewer': {'login': 'factory'}}
        with patch.object(pr_status, 'graphql', return_value=answer):
            reconcile._pr_seen(self.project, pull, conn, ticket.runId)
            with patch.object(store, 'babysit') as wake:
                reconcile._rebabysit(conn, ticket, pull,
                                     pr_status.pull_status(self.project, pull), 0)
                wake.assert_not_called()
                node['commits']['nodes'].append({'commit': {
                    'oid': 'newly-pushed', 'committedDate': '2020-01-01T09:00:00Z',
                    'author': {'user': {'login': 'person'}}}})
                status = pr_status.pull_status(self.project, pull)
                reconcile._rebabysit(conn, ticket, pull, status, 0)
                wake.assert_called_once()
                self.assertIn('commit', wake.call_args.args[2])
                wake.reset_mock()
                reconcile._rebabysit(conn, ticket, pull, status, 0)
                wake.assert_not_called()

    def test_overflow_budget_prevents_dispatch_for_connections_and_replies(self):
        from test_pullrequest import MergeModePullRequestTests as H

        from holophyte import pr_status, reconcile
        H.parked_with_mark(self, H.T1, 0)
        conn = store.open(self.db)
        self.addCleanup(conn.close)
        page = {'nodes': [], 'pageInfo': {
            'hasPreviousPage': True, 'startCursor': 'previous'}}
        comment = {'id': 'new', 'createdAt': H.T2,
                   'author': {'login': 'person'}, 'body': 'Please check'}
        reset = '2099-01-01T00:00:00Z'
        for thread in (False, True):
            with self.subTest(thread=thread):
                node = dict(H.OPEN_PULL, updatedAt=H.T2)
                if thread:
                    node['reviewThreads'] = {'nodes': [
                        {'id': 'thread', 'comments': page}]}
                else:
                    node['comments'] = page
                overflow = {'comments': {'nodes': [comment]}}
                answers = [
                    {'repository': {'pullRequest': node},
                     'rateLimit': {'remaining': 520, 'resetAt': reset}},
                    dict({'node': overflow} if thread else
                         {'repository': {'pullRequest': overflow}},
                         rateLimit={'remaining': 480, 'resetAt': reset})]
                budget = reconcile.GitHubBudget()
                with patch.object(pr_status, 'graphql', side_effect=answers) as read, \
                        patch.object(reconcile, 'GITHUB_BUDGET', budget), \
                        patch.object(store, 'babysit') as wake:
                    reconcile._reconcile_pull_requests(
                        self.project, conn,
                        conn.execute("SELECT id FROM projects").fetchone()[0],
                        StubProvider())
                    wake.assert_not_called()
                    self.assertEqual(budget.remaining, 480)
                    self.assertEqual(budget.reset_at, reset)
                    self.assertIn('rateLimit { remaining resetAt }',
                                  read.call_args.args[2])


class CanceledParkedPullRequestTests(MergeModeFixture):
    """KO-660: a ticket canceled on the board is finished even when its
    newest run holds a pull request; a Done one is still GitHub's."""

    def parked_on_pr(self):
        from test_pullrequest import MergeModePullRequestTests as H
        H.parked_on_pr(self)
        return H

    def board_says(self, state):
        """A provider whose board-state read holds KO-131 in `state`."""
        provider = StubProvider()
        provider.live["KO-131"] = {"board_state": state}
        return provider

    def test_a_canceled_ticket_whose_pr_was_closed_is_abandoned(self):
        H = self.parked_on_pr()
        H.fake_client(self, H.CLOSED_PULL)
        self.main_output(provider=StubProvider())
        self.assertEqual(self.read("SELECT outcome, parkKind FROM runs"),
                         [("rejected", "pull_request_closed")])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("blocked_on_operator",)])

        printed = self.main_output(provider=self.board_says("Canceled"))

        self.assertNotIn("left KO-131 to its pull request", printed)
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("abandoned",)])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("rejected",)])

    def test_a_canceled_ticket_parked_on_an_open_pr_is_closed_out(self):
        H = self.parked_on_pr()
        H.fake_client(self, H.OPEN_PULL)

        printed = self.main_output(provider=self.board_says("Canceled"))

        (run_id,) = self.read("SELECT id FROM runs")[0]
        self.assertEqual(
            self.read('SELECT runId, "action", source, "trigger"'
                      " FROM interventions WHERE runId IS NOT NULL"),
            [(run_id, "close_out", "supervisor", "linear_cancelled")])
        phase, outcome, reason, ended = self.read(
            "SELECT phase, outcome, outcomeReason, endedAt FROM runs")[0]
        self.assertEqual((phase, outcome), ("failed", "abandoned"))
        self.assertIsNotNone(ended)
        self.assertIn("canceled on the board", reason)
        self.assertIn(self.URL, reason)
        # Record before acting: the intervention's event precedes the end.
        seqs = dict(self.read(
            "SELECT kind, seq FROM runEvents WHERE kind = 'intervention'"
            " UNION ALL SELECT 'ended', seq FROM runEvents"
            " WHERE summary LIKE '%outcome abandoned%'"))
        self.assertLess(seqs["intervention"], seqs["ended"])
        self.assertEqual(self.read("SELECT status, activeRunId FROM tickets"),
                         [("abandoned", None)])
        self.assertNotIn("left KO-131 to its pull request", printed)
        line = next(line for line in printed.splitlines()
                    if "canceled on the board" in line)
        self.assertIn(f"{self.URL} left open", line)

    def test_a_done_ticket_parked_on_an_open_pr_is_left_to_it(self):
        H = self.parked_on_pr()
        H.fake_client(self, H.OPEN_PULL)

        printed = self.main_output(provider=self.board_says("Done"))

        self.assertIn("reconcile left KO-131 to its pull request", printed)
        self.assertEqual(self.read("SELECT phase, endedAt FROM runs"),
                         [("awaiting_merge_approval", None)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("blocked_on_operator",)])
        self.assertEqual(self.read("SELECT id FROM interventions"
                                   " WHERE runId IS NOT NULL"), [])

    def test_a_pr_merged_after_the_pull_request_read_lands_not_abandons(self):
        """PR #216 review: the pull request merged between the pass's pull
        request read and the cancel; the cancel asks again and lands it."""
        H = self.parked_on_pr()
        asked = H.fake_client(self, H.OPEN_PULL, H.MERGED_PULL)

        printed = self.main_output(provider=self.board_says("Canceled"))

        self.assertEqual(len(asked), 2)
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("done", "merged", self.MERGE_SHA)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])
        self.assertEqual(self.read('SELECT "action" FROM interventions'
                                   " WHERE runId IS NOT NULL"), [("approve",)])
        self.assertNotIn("left open", printed)
