"""The loop's pass before a claim, driven end to end with zero agent calls.

What the loop does at startup and at each claim before any ticket is
dispatched: the read-only sweep's diagnostics (`_startup_sweep` and the
held-ticket preamble), the startup reconcile of the mirrored board
(`_reconcile_at_startup`) and the queue mirror (`_mirror_queue`). The
harness is `tests/loop_fixture.py`: a real throwaway repo, a real store, a
stub provider, and `tests/fake_agent.py` scripting the agent turns.

Run: python3 -m unittest discover -s tests -p 'test_reconcile*' -v
"""
from __future__ import annotations

import io
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
    StubProvider,
    a_task,
)

import holophyte.board  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets as tickets  # noqa: E402 - after the sys.path insert above


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
