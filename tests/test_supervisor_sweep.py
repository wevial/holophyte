"""Supervisor contract: the stale-run sweep and what it does about a trip.

The loop watches itself only while it is alive; a crashed or hung run leaves a
row in a work phase, a heartbeat that stopped and a lease nobody gives back.
These tests assert what the sweep notices about such a run and, just as
importantly, what it refuses to notice: one silent sighting is not evidence, a
run inside its budget is not late, and a finished or parked run is not swept at
all. The clock is a parameter throughout, so every age below is arithmetic
rather than a sleep.

The acting half (`--act`) is asserted the same way: by the state a human and
the next loop invocation find afterwards -- a failed run, released leases, a
worktree still on disk, and a failure that counts towards the ticket's
escalation threshold like any the loop recorded itself.

Run: python3 -m unittest discover -s tests -p 'test_supervisor*' -v
"""
from __future__ import annotations

import io
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
import holophyte.cli  # noqa: E402 - after the sys.path insert above
import holophyte.config  # noqa: E402 - after the sys.path insert above
import holophyte.runs  # noqa: E402 - after the sys.path insert above
import holophyte.supervisor  # noqa: E402 - after the sys.path insert above
import holophyte.target  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above

MINUTE = 60 * 1000
T0 = 1_700_000_000_000  # an epoch-millisecond wall clock the tests do sums on


class Tripwire:
    """Stands in for a module nothing may touch: every attribute raises."""

    def __init__(self, what):
        object.__setattr__(self, "what", what)

    def __getattr__(self, name):
        raise AssertionError(f"{self.what}.{name} was reached")


def no_network():
    """Fail any attempt to open a socket, at the one call urllib makes."""
    def refuse(*args, **kwargs):
        raise AssertionError("a network connection was attempted")

    return patch.multiple(socket, socket=refuse, create_connection=refuse)


class SweepTestCase(unittest.TestCase):
    """A store with one project, and runs the test places in time by hand."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.target = self.root / "repo"
        self.target.mkdir()
        # Where `Target.locate(self.target)` will look: the target's directory
        # under a HOLOPHYTE_HOME of this test's own, never the operator's real
        # one.
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.root / "home")})
        home.start()
        self.addCleanup(home.stop)
        self.db = holophyte.target.state_dir(self.target) / "store.db"
        self.db.parent.mkdir(parents=True)
        # The `Target` every sweep here is handed. The acting sweep writes
        # FINDINGS.md into whichever target it names, so it is this test's
        # repository and never the one this suite is running in.
        self.tgt = holophyte.target.Target.locate(self.target)
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.projects = 1
        self.project = store.ensure_project(self.conn, "team-1", self.target)
        self.tickets = 0
        self.ticket_of = {}

    def another_project(self):
        """A second project, for the tests that need two runs live at once.

        v0 single-threads a project, so `a_run()` twice over one of them is a
        `ClaimConflict` rather than the pair of live runs the test wanted.
        """
        self.projects += 1
        repo = self.root / f"repo-{self.projects}"
        repo.mkdir()
        return store.ensure_project(self.conn, f"team-{self.projects}", repo)

    def a_run(self, budget_min=25, claimed_at=T0, phase="working",
              project=None, ticket=None):
        """One live run of its own ticket, claimed at `claimed_at`.

        `claim()` stamps `startedAt` and `lastHeartbeat` together, so a fresh
        run's heartbeat is its claim time until a stage boundary moves it;
        `heartbeat_at()` below is how a test moves it.

        The ticket is specced and moved to `in_flight`, which is what the loop
        does to a ticket it claims: a run in flight whose ticket says anything
        else is a store state the loop cannot produce, and the escalation the
        acting sweep feeds is only defined on an in-flight ticket. `ticket`
        re-claims one an earlier run already used, for the tests about a
        ticket's second failure.
        """
        project = self.project if project is None else project
        if ticket is None:
            self.tickets += 1
            n = self.tickets
            ticket = store.mirror_ticket(
                self.conn, project, linear_issue_id=f"issue-{n}",
                linear_identifier=f"KO-{n}", title=f"ticket {n}",
                acceptance_criteria=[f"Given ticket {n}, then it is worked"],
                verification_commands=["echo ok"],
                time_box_ms=budget_min and budget_min * MINUTE)
            store.transition(self.conn, ticket, "in_flight")
        run_id = store.claim(self.conn, project, ticket, now=claimed_at)
        self.ticket_of[run_id] = ticket
        if phase != "claimed":
            store.set_phase(self.conn, run_id, phase, now=claimed_at)
        return run_id

    def heartbeat_at(self, run_id, at):
        """Move the run's last heartbeat, the way a stage boundary would."""
        store.set_phase(self.conn, run_id, store.run_phase(self.conn, run_id),
                        now=at)

    def run_sweep(self, at, *flags):
        """The mode end to end, with the provider and the network as tripwires.

        The mode reads the wall clock, which is the one thing about it a test
        cannot arrange, so `time` is what `at` replaces -- the seam the sweep
        itself takes as a parameter.
        """
        out = io.StringIO()
        with patch.dict(sys.modules,
                        {"linear_provider": Tripwire("linear_provider")}):
            with no_network(), patch.object(sys, "stdout", out), \
                    patch.object(holophyte.supervisor, "time", lambda: at / 1000):
                holophyte.cli.cli(["--sweep", *flags, str(self.target)])
        return out.getvalue().splitlines()

    def strikes(self, run_id):
        """The strike row the sweep keeps, read straight out of the table."""
        row = self.conn.execute(
            "SELECT strikes, lastSeen FROM sweepStrikes WHERE runId = ?",
            (run_id,)).fetchone()
        return row


class StaleHeartbeatTests(SweepTestCase):
    """Liveness: one silent sighting is a strike, two in a row is a trip."""

    def test_a_fresh_heartbeat_inside_its_budget_does_not_trip(self):
        run_id = self.a_run(budget_min=25)

        result = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 2 * MINUTE)

        self.assertEqual(result.trips, [])
        self.assertEqual(result.swept, 1)
        self.assertIsNone(self.strikes(run_id))

    def test_one_stale_sighting_is_a_strike_and_not_a_trip(self):
        """The two-strike rule's whole point: a load spike is not a death."""
        run_id = self.a_run()

        result = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 6 * MINUTE)

        self.assertEqual(result.trips, [])
        self.assertEqual(self.strikes(run_id), (1, T0 + 6 * MINUTE))

    def test_a_first_strike_is_watched_and_printed_not_healthy(self):
        """One silent sighting is not a trip — but printing 'all healthy'
        over it hid the evidence from the operator whose relaunch reflex
        the KO-146 incident documented. The suspicion is carried and
        rendered, with the strike count naming what happens next."""
        run_id = self.a_run()

        result = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 6 * MINUTE)

        self.assertEqual(result.trips, [])
        (line,) = result.watched
        self.assertIn(f"run {run_id}", line)
        self.assertIn("strike 1 of 2", line)
        printed = holophyte.supervisor.sweep_lines(result)
        self.assertEqual(printed[0], "1 run swept, none tripped")
        self.assertIn(line, printed)

    def test_a_second_sighting_seconds_later_is_the_same_sample(self):
        """Two launches in a minute are one observation of one silence: a
        healthy implementer turn is routinely 'silent' past the stale
        threshold, and rapid relaunches must not manufacture the second
        strike that lets --sweep --act fail a live run."""
        run_id = self.a_run()

        holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 6 * MINUTE)
        result = holophyte.supervisor.sweep(self.tgt, self.conn,
                                            T0 + 6 * MINUTE + 20_000)

        self.assertEqual(result.trips, [])
        (line,) = result.watched
        self.assertIn("strike 1 of 2", line)
        self.assertEqual(self.strikes(run_id), (1, T0 + 6 * MINUTE))

    def test_two_consecutive_stale_sightings_trip_the_run(self):
        run_id = self.a_run(phase="reviewing")

        holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 6 * MINUTE)
        result = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 12 * MINUTE)

        trip, = result.trips
        self.assertEqual(
            (trip.run_id, trip.ticket, trip.phase, trip.condition),
            (run_id, "KO-1", "reviewing", "stale_heartbeat"))
        # The age the verdict was reached on, so a reader can agree with it:
        # twelve minutes of silence, seen twice a proper interval apart.
        self.assertIn("12.0 min", trip.evidence)
        self.assertIn("2 consecutive sweeps", trip.evidence)

    def test_a_heartbeat_between_sweeps_clears_the_count(self):
        """Consecutive, not cumulative: a run that answers starts over."""
        run_id = self.a_run()

        holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 6 * MINUTE)
        self.heartbeat_at(run_id, T0 + 7 * MINUTE)
        alive = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 8 * MINUTE)
        stale_again = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 14 * MINUTE)

        self.assertEqual(alive.trips, [])
        # Silent again six minutes later, and back to a first strike rather
        # than the second one that would have tripped it.
        self.assertEqual(stale_again.trips, [])
        self.assertEqual(self.strikes(run_id), (1, T0 + 14 * MINUTE))

    def test_a_heartbeat_no_sweep_saw_fresh_still_clears_the_count(self):
        """The tally counts consecutive silence, not consecutive sweeps.

        A run heartbeating a little slower than the sweep runs is caught
        silent every time and seen fresh by none of them, and counting
        sightings alone would trip it while it is alive and answering.
        """
        run_id = self.a_run()

        first = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 6 * MINUTE)
        self.heartbeat_at(run_id, T0 + 7 * MINUTE)  # alive, between sweeps
        second = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 13 * MINUTE)

        # Six minutes silent again, so a strike again -- but the first one,
        # because the run answered after it was recorded.
        self.assertEqual((first.trips, second.trips), ([], []))
        self.assertEqual(self.strikes(run_id), (1, T0 + 13 * MINUTE))

    def test_silence_unbroken_across_sweeps_still_trips(self):
        """The recovery check is a heartbeat newer than the strike on file,
        not a rule that every second sighting is forgiven."""
        run_id = self.a_run()

        holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 6 * MINUTE)
        holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 12 * MINUTE)
        trip, = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 18 * MINUTE).trips

        self.assertEqual((trip.run_id, trip.condition),
                         (run_id, "stale_heartbeat"))
        self.assertEqual(self.strikes(run_id), (3, T0 + 18 * MINUTE))


class TimeBoxTests(SweepTestCase):
    """The budget trip: wall clock since the claim, against the run's box."""

    def test_a_run_past_the_grace_multiple_of_its_budget_trips(self):
        run_id = self.a_run(budget_min=20)
        at = T0 + 31 * MINUTE  # 1.55x of 20 min
        self.heartbeat_at(run_id, at)  # alive, and still overdue

        trip, = holophyte.supervisor.sweep(self.tgt, self.conn, at).trips

        self.assertEqual((trip.run_id, trip.condition), (run_id, "time_box"))
        self.assertIn("31.0 min", trip.evidence)
        self.assertIn("20 min box", trip.evidence)

    def test_a_run_inside_the_grace_multiple_does_not_trip(self):
        """Over its estimate is not overdue: the grace is 1.5x, not 1x."""
        run_id = self.a_run(budget_min=20)
        at = T0 + 29 * MINUTE  # 1.45x of 20 min
        self.heartbeat_at(run_id, at)

        self.assertEqual(holophyte.supervisor.sweep(self.tgt, self.conn, at).trips, [])

    def test_a_run_claimed_against_no_estimate_has_no_box_to_blow(self):
        run_id = self.a_run(budget_min=None)
        at = T0 + 600 * MINUTE
        self.heartbeat_at(run_id, at)

        self.assertEqual(holophyte.supervisor.sweep(self.tgt, self.conn, at).trips, [])


def finding(path, severity="p1", line=1):
    return {"path": path, "line": line, "severity": severity,
            "message": f"something about {path}"}


class ReviewStuckTests(SweepTestCase):
    """The review-stuck trip: two finished rounds whose findings overlap.

    Rounds go in through `store.record_review_round()`, the writer the loop
    uses, so what is compared is the store's own row and not a fixture's idea
    of one. Every run here heartbeats at the sweep and sits inside its budget:
    a stuck review is a trip on a run that is alive and on time.
    """

    def round(self, run_id, number, findings, at=T0 + MINUTE, ended=True):
        verdict = "changes_requested" if findings else "pass"
        store.record_review_round(
            self.conn, run_id, number, verdict, "reviewer",
            findings=findings, started_at=at,
            ended_at=at + MINUTE if ended else None)

    def sweep(self, run_id, at=T0 + 10 * MINUTE):
        """A sweep at `at` of a run alive at `at`."""
        self.heartbeat_at(run_id, at)
        return holophyte.supervisor.sweep(self.tgt, self.conn, at)

    def test_two_rounds_sharing_no_findings_do_not_trip(self):
        """A fix round that cleared every complaint and drew new ones is a
        review moving, however many findings it has on file."""
        run_id = self.a_run(phase="addressing")
        self.round(run_id, 1, [finding("a.py"), finding("b.py")])
        self.round(run_id, 2, [finding("c.py"), finding("d.py")])

        self.assertEqual(self.sweep(run_id).trips, [])

    def test_two_rounds_with_identical_findings_trip_with_the_overlap(self):
        run_id = self.a_run(phase="addressing")
        same = [finding("a.py"), finding("b.py", "p2", line=7)]
        self.round(run_id, 1, same)
        self.round(run_id, 2, list(reversed(same)), at=T0 + 3 * MINUTE)

        trip, = self.sweep(run_id).trips

        self.assertEqual(
            (trip.run_id, trip.ticket, trip.phase, trip.condition),
            (run_id, "KO-1", "addressing", "review_stuck"))
        # Both round numbers and the overlap value, so a reader can agree.
        self.assertIn("rounds 1 and 2", trip.evidence)
        self.assertIn("1.00", trip.evidence)

    def test_overlap_at_the_threshold_trips_and_below_it_does_not(self):
        """The threshold is on the Jaccard measure: two of three findings
        kept is 2/4, which is the line; one of three kept is 1/5, under it."""
        at_line = self.a_run(phase="reviewing")
        self.round(at_line, 1, [finding("a.py"), finding("b.py"), finding("c.py")])
        self.round(at_line, 2, [finding("a.py"), finding("b.py"), finding("d.py")])
        under = self.a_run(phase="reviewing", project=self.another_project())
        self.round(under, 1, [finding("a.py"), finding("b.py"), finding("c.py")])
        self.round(under, 2, [finding("a.py"), finding("d.py"), finding("e.py")])
        self.heartbeat_at(at_line, T0 + 10 * MINUTE)

        result = self.sweep(under)

        self.assertEqual([(t.run_id, t.condition) for t in result.trips],
                         [(at_line, "review_stuck")])
        self.assertIn("0.50", result.trips[0].evidence)

    def test_rounds_with_empty_findings_never_trip(self):
        """Equal empty sets measure 1.0, and must not read as repetition: a
        pass after a pass is a review with nothing left to say."""
        run_id = self.a_run(phase="reviewing")
        self.round(run_id, 1, [])
        self.round(run_id, 2, [])
        one_sided = self.a_run(phase="reviewing", project=self.another_project())
        self.round(one_sided, 1, [finding("a.py")])
        self.round(one_sided, 2, [])
        self.heartbeat_at(run_id, T0 + 10 * MINUTE)

        self.assertEqual(self.sweep(one_sided).trips, [])

    def test_one_round_is_not_compared_against_anything(self):
        """A healthy run legitimately sits in `reviewing` with round 1 on
        file, and an unfinished round 2 is not a round yet."""
        run_id = self.a_run(phase="reviewing")
        self.round(run_id, 1, [finding("a.py")])
        self.round(run_id, 2, [finding("a.py")], at=T0 + 3 * MINUTE,
                   ended=False)

        self.assertEqual(self.sweep(run_id).trips, [])

    def test_a_run_past_its_review_is_not_tripped_by_its_history(self):
        """Two overlapping rounds are only a stuck review while the run is
        still in one: a run that got through to merging is not circling."""
        run_id = self.a_run(phase="merging")
        self.round(run_id, 1, [finding("a.py")])
        self.round(run_id, 2, [finding("a.py")], at=T0 + 3 * MINUTE)

        self.assertEqual(self.sweep(run_id).trips, [])

    def test_an_acting_sweep_fails_a_stuck_review_like_any_other_trip(self):
        """The trip flows through 2/5's close-out unchanged: failed run,
        released leases, the condition in the run's own stream."""
        run_id = self.a_run(phase="addressing")
        self.round(run_id, 1, [finding("a.py")])
        self.round(run_id, 2, [finding("a.py")], at=T0 + 3 * MINUTE)
        self.heartbeat_at(run_id, T0 + 10 * MINUTE)

        result = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 10 * MINUTE,
                                            act=True)

        self.assertEqual(len(result.trips), 1)
        phase, outcome, ended, reason = self.conn.execute(
            "SELECT phase, outcome, endedAt, outcomeReason FROM runs"
            " WHERE id = ?", (run_id,)).fetchone()
        self.assertEqual((phase, outcome), ("failed", "failed"))
        self.assertIsNotNone(ended)
        self.assertIn("review_stuck", reason)
        self.assertIn("addressing", reason)
        (project,) = self.conn.execute(
            "SELECT activeRunId FROM projects WHERE id = ?",
            (self.project,)).fetchone()
        self.assertIsNone(project)
        self.assertEqual(self.conn.execute(
            "SELECT activeRunId, lastRunId FROM tickets WHERE id = ?",
            (self.ticket_of[run_id],)).fetchone(), (None, run_id))
        (event,) = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind = ?",
            (run_id, holophyte.supervisor.SWEEP_EVENT)).fetchall()
        self.assertIn("rounds 1 and 2", event[0])

    def test_a_round_that_converged_since_the_verdict_acquits_the_run(self):
        """The verdict was reached on rounds 1 and 2; by the time the sweep
        acts, round 3 has ended and cleared the overlap. The run went through
        `addressing` and back, so it sits in the phase the trip named -- the
        phase check alone would fail a review that has just moved."""
        run_id = self.a_run(phase="reviewing")
        self.round(run_id, 1, [finding("a.py"), finding("b.py")])
        self.round(run_id, 2, [finding("a.py"), finding("b.py")],
                   at=T0 + 3 * MINUTE)
        self.heartbeat_at(run_id, T0 + 10 * MINUTE)
        trip, = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 10 * MINUTE).trips
        self.assertEqual(trip.condition, "review_stuck")
        # The loop's own process, in the gap after the verdict committed.
        self.round(run_id, 3, [finding("c.py")], at=T0 + 11 * MINUTE)

        outcome = holophyte.supervisor.act_on_trip(self.tgt, self.conn, trip)

        self.assertFalse(outcome.acted)
        self.assertEqual(outcome.phase, "reviewing")
        self.assertEqual(self.conn.execute(
            "SELECT phase, endedAt FROM runs WHERE id = ?",
            (run_id,)).fetchone(), ("reviewing", None))
        # The supervisor's visit is on the record as a decline, not a
        # failure: the stream says it looked and stood down.
        (event,) = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind = ?",
            (run_id, holophyte.supervisor.SWEEP_EVENT)).fetchall()
        self.assertIn("no action", event[0])
        self.assertNotIn("failing", event[0])

    def test_a_round_that_still_overlaps_since_the_verdict_does_not_acquit(self):
        """The converse: a round 3 that repeats round 2 is the same stuck
        review with one more round on file, and the verdict stands."""
        run_id = self.a_run(phase="reviewing")
        self.round(run_id, 1, [finding("a.py"), finding("b.py")])
        self.round(run_id, 2, [finding("a.py"), finding("b.py")],
                   at=T0 + 3 * MINUTE)
        self.heartbeat_at(run_id, T0 + 10 * MINUTE)
        trip, = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 10 * MINUTE).trips
        self.round(run_id, 3, [finding("a.py"), finding("b.py")],
                   at=T0 + 11 * MINUTE)

        self.assertTrue(
            holophyte.supervisor.act_on_trip(self.tgt, self.conn, trip).acted)
        self.assertEqual(self.conn.execute(
            "SELECT phase FROM runs WHERE id = ?", (run_id,)).fetchone(),
            ("failed",))

    def test_reaching_terminal_adjudication_on_the_same_rounds_does_not_acquit(self):
        """The verdict was reached in `reviewing` on rounds 1 and 2; by the
        time the sweep acts, the run has been through `addressing` and
        `verifying` and is back in `reviewing` for its terminal adjudication,
        with no new round on file because that one has not ended. The
        adjudication is what the trip exists to spare: the evidence is the
        same two rounds a fresh sweep would trip on, and the verdict stands."""
        run_id = self.a_run(phase="reviewing")
        self.round(run_id, 1, [finding("a.py"), finding("b.py")])
        self.round(run_id, 2, [finding("a.py"), finding("b.py")],
                   at=T0 + 3 * MINUTE)
        self.heartbeat_at(run_id, T0 + 10 * MINUTE)
        trip, = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 10 * MINUTE).trips
        self.assertEqual(trip.condition, "review_stuck")
        for phase in ("addressing", "verifying", "reviewing"):
            store.set_phase(self.conn, run_id, phase, now=T0 + 11 * MINUTE)

        self.assertTrue(
            holophyte.supervisor.act_on_trip(self.tgt, self.conn, trip).acted)
        self.assertEqual(self.conn.execute(
            "SELECT phase FROM runs WHERE id = ?", (run_id,)).fetchone(),
            ("failed",))
        # And a sweep arriving fresh at that moment reads the same run the
        # same way, so the confirmed verdict changed nothing but the timing.
        fresh = self.a_run(phase="reviewing", project=self.another_project())
        self.round(fresh, 1, [finding("a.py"), finding("b.py")])
        self.round(fresh, 2, [finding("a.py"), finding("b.py")],
                   at=T0 + 3 * MINUTE)
        self.heartbeat_at(fresh, T0 + 12 * MINUTE)
        trip, = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 12 * MINUTE).trips
        self.assertEqual((trip.run_id, trip.condition), (fresh, "review_stuck"))


class NotSweptTests(SweepTestCase):
    """Rows the sweep must leave alone, however old their heartbeats are."""

    def test_an_ended_run_is_not_swept(self):
        """A finished run's heartbeat stopped because the work stopped."""
        done = self.a_run()
        store.release(self.conn, done, "merged", now=T0 + MINUTE)
        live = self.a_run(claimed_at=T0 + 2 * MINUTE)

        result = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 20 * MINUTE)

        self.assertEqual(result.swept, 1)
        self.assertEqual([trip.run_id for trip in result.trips], [])
        self.assertIsNone(self.strikes(done))
        self.assertEqual(self.strikes(live)[0], 1)

    def test_a_run_parked_for_an_operator_is_not_swept(self):
        """It has no heartbeat by design: it is waiting for a human answer."""
        parked = self.a_run(phase="blocked_on_operator")

        first = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 6 * MINUTE)
        second = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 12 * MINUTE)

        self.assertEqual((first.swept, second.swept), (0, 0))
        self.assertEqual(second.trips, [])
        self.assertIsNone(self.strikes(parked))


class AtomicityTests(SweepTestCase):
    """The sweep watches a process that is writing the columns it reads."""

    def rival(self):
        """A second connection to the same store, as the loop's process is.

        `timeout=0` so a lock it cannot take fails instantly instead of
        waiting out the sweep -- the test wants the answer, not the wait.
        """
        conn = sqlite3.connect(str(self.db), timeout=0)
        self.addCleanup(conn.close)
        return conn

    def test_a_heartbeat_cannot_land_between_the_verdict_and_the_strike(self):
        """Classify and record are one instant, so no strike is stamped on a
        state that stopped being true a millisecond after it was read."""
        run_id = self.a_run()
        loop = self.rival()
        real = store.record_strike
        raced = []

        def strike_and_race(conn, rid, stale, heartbeat, now=None):
            # The moment the finding is about: the verdict is made, the strike
            # is about to be written, and the run answers in between.
            try:
                loop.execute("BEGIN IMMEDIATE")
                loop.execute("UPDATE runs SET lastHeartbeat = ? WHERE id = ?",
                             (now, rid))
                loop.commit()
                raced.append(None)
            except sqlite3.OperationalError as refused:
                loop.rollback()
                raced.append(str(refused))
            return real(conn, rid, stale, heartbeat, now)

        with patch.object(store, "record_strike", strike_and_race):
            result = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 6 * MINUTE)

        # Refused, not interleaved: the sweep holds the write lock across both
        # halves, so the loop's heartbeat waits for a sweep that is over.
        self.assertEqual(len(raced), 1)
        self.assertIsNotNone(raced[0], "a heartbeat committed mid-sweep")
        self.assertIn("locked", raced[0])
        self.assertEqual(result.trips, [])
        self.assertEqual(self.strikes(run_id), (1, T0 + 6 * MINUTE))

    def test_the_heartbeat_the_sweep_shut_out_lands_once_it_is_over(self):
        """The lock is held for the pass, not for the supervisor's lifetime."""
        run_id = self.a_run()
        loop = self.rival()

        holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 6 * MINUTE)
        loop.execute("BEGIN IMMEDIATE")
        loop.execute("UPDATE runs SET lastHeartbeat = ? WHERE id = ?",
                     (T0 + 6 * MINUTE, run_id))
        loop.commit()

        # And the next sweep sees it and clears the strike, which is the
        # behaviour the shut-out heartbeat was queued for.
        self.assertEqual(
            holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 7 * MINUTE).trips, [])
        self.assertIsNone(self.strikes(run_id))

    def test_a_failed_sweep_writes_no_strikes_at_all(self):
        """One transaction, so a pass that dies half-way leaves no half-tally.

        Without it the first run's strike is committed and the second's is
        not, and the next sweep trips the one the crash happened to precede.
        """
        first = self.a_run()
        second = self.a_run(project=self.another_project())
        real = store.record_strike

        def strike_then_die(conn, rid, stale, heartbeat, now=None):
            strikes = real(conn, rid, stale, heartbeat, now)
            if rid == second:
                raise RuntimeError("the sweep died mid-pass")
            return strikes

        with patch.object(store, "record_strike", strike_then_die):
            with self.assertRaises(RuntimeError):
                holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 6 * MINUTE)

        self.assertIsNone(self.strikes(first))
        self.assertIsNone(self.strikes(second))


class StubProvider:
    """The board, recording what it is told rather than telling Linear.

    Only the escalating sweep needs one: a swept failure below the threshold
    pushes no status and comments on nothing, which is why the other acting
    tests can leave the provider as the tripwire it is in the mode tests.
    """

    def __init__(self):
        self.states = []
        self.comments = []

    def set_state(self, issue_id, state):
        self.states.append((issue_id, state))

    def comment(self, issue_id, body):
        self.comments.append((issue_id, body))


class ActingSweepTests(SweepTestCase):
    """What an acting sweep does to the runs it trips, and to the rest.

    The close-out is the loop's own, so what is asserted here is the state a
    human and the next loop invocation find afterwards: a failed run, freed
    leases, a preserved worktree, and a failure that counts like every other.
    """

    def setUp(self):
        super().setUp()
        # The acting close-out renders FINDINGS.md into the module's target,
        # which is the repository this suite runs in until it is moved.

    def act(self, at, provider=None):
        """One acting sweep at `at`, as `--sweep --act` runs it."""
        return holophyte.supervisor.sweep(self.tgt, self.conn, at, act=True,
                                          provider=provider)

    def trip(self):
        """Take the first strike, and return the time the second one trips.

        Two consecutive silent sightings, a proper interval apart, are what
        a stale heartbeat is -- so every acting test needs a read-only sweep
        before the acting one, and the second sighting sits beyond the
        minimum spacing.
        """
        holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 6 * MINUTE)
        return T0 + 12 * MINUTE

    def run_row(self, run_id):
        return self.conn.execute(
            "SELECT phase, outcome, endedAt, outcomeReason FROM runs"
            " WHERE id = ?", (run_id,)).fetchone()

    def leases(self, run_id):
        """Both `activeRunId` columns, and where the ticket's pointer went."""
        (project,) = self.conn.execute(
            "SELECT activeRunId FROM projects WHERE id = ?",
            (self.project,)).fetchone()
        return (project,) + self.conn.execute(
            "SELECT activeRunId, lastRunId FROM tickets WHERE id = ?",
            (self.ticket_of[run_id],)).fetchone()

    def status(self, run_id):
        (status,) = self.conn.execute(
            "SELECT status FROM tickets WHERE id = ?",
            (self.ticket_of[run_id],)).fetchone()
        return status

    def test_a_tripped_run_is_failed_and_both_leases_released(self):
        """The whole point: the queue is unblocked without a human. The run
        ends as a failure naming what tripped it, the project stops holding a
        lease for a process that is gone, and the ticket keeps a pointer to
        the run that failed on it."""
        run_id = self.a_run()
        at = self.trip()

        result = self.act(at)

        self.assertEqual(len(result.trips), 1)
        phase, outcome, ended, reason = self.run_row(run_id)
        self.assertEqual((phase, outcome), ("failed", "failed"))
        # Stamped by the close-out's own clock: the sweep's `now` is when the
        # run was *declared* dead, and the run ended whenever it stopped
        # writing -- neither is the other, so the ending is not backdated.
        self.assertIsNotNone(ended)
        self.assertIn("stale_heartbeat", reason)
        self.assertIn("working", reason)  # the phase it was swept in
        self.assertEqual(self.leases(run_id), (None, None, run_id))
        # One failure is not a pattern, so the ticket is still open work.
        self.assertEqual(self.status(run_id), "in_flight")

    def test_the_trip_condition_is_recorded_where_a_human_will_read_it(self):
        """A freed lease with no account of why is a mystery in the morning:
        the run's own event stream says the supervisor arrived, and the
        rendered window names the condition beside the run it ended."""
        run_id = self.a_run(phase="reviewing")
        at = self.trip()

        self.act(at)

        (event,) = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind = ?",
            (run_id, holophyte.supervisor.SWEEP_EVENT)).fetchall()
        self.assertIn("stale_heartbeat", event[0])
        self.assertIn("2 consecutive sweeps", event[0])
        rendered = (self.target / "FINDINGS.md").read_text()
        self.assertIn("KO-1", rendered)
        self.assertIn("stale_heartbeat", rendered)

    def test_a_healthy_run_is_untouched_by_an_acting_sweep(self):
        """`--act` acts on trips, not on runs: a loop that is working must be
        able to have a supervisor pointed at it."""
        run_id = self.a_run()
        before = self.run_row(run_id)
        events = self.conn.execute(
            "SELECT COUNT(*) FROM runEvents").fetchone()

        result = self.act(T0 + 2 * MINUTE)

        self.assertEqual(result.trips, [])
        self.assertEqual(self.run_row(run_id), before)
        self.assertEqual(self.leases(run_id), (run_id, run_id, None))
        self.assertEqual(self.status(run_id), "in_flight")
        # No writes beyond the strike bookkeeping: no event, and no rendered
        # window, which the close-out would have written into the target.
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM runEvents").fetchone(),
            events)
        self.assertFalse((self.target / "FINDINGS.md").exists())

    def test_a_run_that_ended_before_the_act_is_declined_and_reported_so(self):
        """holophyte-bugs.md #1: the loop's own process finished the run in
        the gap between the classification committing and the act. The
        re-check stands down, and the summary must say so -- naming the
        status it saw -- rather than claim a failure that was never written.
        The store shows the supervisor looked, so a reader of the run's
        stream sees the visit and the decision, not a silent no-op."""
        run_id = self.a_run()
        at = self.trip()
        original_act = holophyte.supervisor.act_on_trip

        def finish_then_act(target, conn, trip, provider=None, knobs=None):
            # The run's own process, landing after the verdict committed.
            store.release(conn, trip.run_id, "merged", now=at)
            return original_act(target, conn, trip, provider, knobs)

        with patch.object(holophyte.supervisor, "act_on_trip", finish_then_act):
            result = self.act(at)
        lines = holophyte.supervisor.sweep_lines(result)

        self.assertEqual(len(result.trips), 1)
        self.assertEqual(self.run_row(run_id)[:3], ("done", "merged", at))
        self.assertIn(f"declined: run {run_id} is now done; no action", lines)
        self.assertNotIn(f"acted: failed run {run_id}, leases released",
                         lines)
        self.assertEqual(lines[-1],
                         "1 tripped of 1 run swept, 1 declined, no action")
        (event,) = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind = ?",
            (run_id, holophyte.supervisor.SWEEP_EVENT)).fetchall()
        self.assertIn("no action", event[0])
        self.assertNotIn("failing", event[0])

    def test_one_acted_and_one_declined_trip_are_counted_apart(self):
        """Two trips in one sweep, one of which the re-check stands down on:
        each gets its own outcome line and the summary counts them apart."""
        other = self.another_project()
        failed = self.a_run()
        finished = self.a_run(project=other)
        at = self.trip()
        original_act = holophyte.supervisor.act_on_trip

        def finish_one_then_act(target, conn, trip, provider=None, knobs=None):
            if trip.run_id == finished:
                store.release(conn, trip.run_id, "merged", now=at)
            return original_act(target, conn, trip, provider, knobs)

        with patch.object(holophyte.supervisor, "act_on_trip", finish_one_then_act):
            lines = holophyte.supervisor.sweep_lines(self.act(at))

        self.assertIn(f"acted: failed run {failed}, leases released", lines)
        self.assertIn(f"declined: run {finished} is now done; no action",
                      lines)
        self.assertEqual(
            lines[-1],
            "2 tripped of 2 runs swept, 1 failed and leases released,"
            " 1 declined, no action")
        self.assertEqual(self.run_row(failed)[:2], ("failed", "failed"))
        self.assertEqual(self.run_row(finished)[:2], ("done", "merged"))

    def test_a_swept_failure_counts_towards_the_escalation_threshold(self):
        """A run the supervisor failed is a failed run like any other, so the
        ticket the loop kept failing on is parked after the second one rather
        than being offered back forever."""
        first = self.a_run()
        store.release(self.conn, first, "failed", "the loop gave up",
                      now=T0 + MINUTE)
        second = self.a_run(claimed_at=T0 + 2 * MINUTE,
                            ticket=self.ticket_of[first])
        holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 8 * MINUTE)
        provider = StubProvider()

        self.act(T0 + 14 * MINUTE, provider)

        self.assertEqual(self.status(second), "blocked_on_operator")
        self.assertEqual(provider.states, [("issue-1", "Todo")])
        (issue_id, body), = provider.comments
        self.assertEqual(issue_id, "issue-1")
        # Both attempts are accounted for, the swept one by its trip condition
        # rather than by a reason invented for the comment.
        self.assertIn("attempt 1: the loop gave up", body)
        self.assertIn("attempt 2: swept by the supervisor", body)
        self.assertIn("stale_heartbeat", body)

    def test_a_board_that_is_down_does_not_abort_the_sweep(self):
        """The escalating push is the one call an acting sweep makes off the
        machine, and an unattended supervisor cannot be stopped by it: the run
        is failed and the lease is freed whatever Linear says, because the
        store is the truth and the board is a copy."""
        first = self.a_run()
        store.release(self.conn, first, "failed", "the loop gave up",
                      now=T0 + MINUTE)
        second = self.a_run(claimed_at=T0 + 2 * MINUTE,
                            ticket=self.ticket_of[first])
        holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 8 * MINUTE)
        provider = StubProvider()

        def refuse(issue_id, state):
            raise RuntimeError("linear is down")

        provider.set_state = refuse

        self.act(T0 + 14 * MINUTE, provider)

        self.assertEqual(self.run_row(second)[:2], ("failed", "failed"))
        self.assertEqual(self.leases(second), (None, None, second))
        (warning,), = self.conn.execute(
            "SELECT summary FROM runEvents WHERE kind = 'warning'").fetchall()
        self.assertIn("linear is down", warning)


class SweepModeTests(SweepTestCase):
    """`factory.py --sweep <target>` as an operator runs it."""

    def test_a_clean_sweep_says_so_rather_than_printing_nothing(self):
        """Silence is ambiguous: an operator cannot tell it from a crash."""
        self.a_run()
        self.conn.commit()

        printed = self.run_sweep(T0 + 2 * MINUTE)

        self.assertEqual(printed, ["1 run swept, all healthy"])

    def test_a_tripped_run_is_printed_and_nothing_is_claimed(self):
        run_id = self.a_run()
        self.conn.commit()

        first = self.run_sweep(T0 + 6 * MINUTE)
        printed = self.run_sweep(T0 + 12 * MINUTE)

        self.assertEqual(first[0], "1 run swept, none tripped")
        self.assertIn("strike 1 of 2", first[1])
        self.assertEqual(printed[0].split(), list(holophyte.supervisor.SWEEP_HEADERS))
        self.assertEqual(printed[1].split()[:5],
                         ["KO-1", "run", str(run_id), "working",
                          "stale_heartbeat"])
        # A store read from another machine has to say where the run is:
        # the trip line and the watched line both end in the host.
        self.assertEqual(printed[1].split()[-1], socket.gethostname())
        self.assertTrue(first[1].endswith(f" on {socket.gethostname()}"),
                        first[1])
        self.assertEqual(printed[-1], "1 tripped of 1 run swept")
        # Read-only apart from the strikes: the run is still in flight, in the
        # phase it stopped in, and the lease it holds was not given back.
        self.assertEqual(
            self.conn.execute(
                "SELECT phase, endedAt, outcome FROM runs WHERE id = ?",
                (run_id,)).fetchone(), ("working", None, None))
        self.assertEqual(
            self.conn.execute(
                "SELECT activeRunId FROM projects").fetchone()[0], run_id)

    def a_worktree(self, branch="task/ko-1"):
        """A real branch and worktree, as a run in flight leaves behind.

        The claim about `--act` that only the filesystem can witness is that
        it preserves them: the run is failed in the store and its work is
        still on disk for a human to look at.
        """
        git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.target,
                       check=True)
        (self.target / "README.md").write_text("holophyte\n")
        subprocess.run(git + ["add", "-A"], cwd=self.target, check=True)
        subprocess.run(git + ["commit", "-qm", "first"], cwd=self.target,
                       check=True)
        wt = self.tgt.worktrees / "ko-1"
        subprocess.run(["git", "worktree", "add", "-q", "-b", branch, str(wt)],
                       cwd=self.target, check=True)
        (wt / "work.txt").write_text("half-finished\n")
        return wt, branch

    def branches(self):
        return subprocess.run(["git", "branch", "--format=%(refname:short)"],
                              cwd=self.target, capture_output=True, text=True,
                              check=True).stdout.split()

    def test_an_acting_sweep_fails_the_run_and_leaves_the_work_on_disk(self):
        """`--sweep --act` as the operator of an overnight run uses it: the
        report says what it did, the store is unblocked, and the branch and
        worktree of the run it failed are untouched -- the supervisor frees
        the queue, a human decides what happens to the work."""
        run_id = self.a_run()
        wt, branch = self.a_worktree()
        self.conn.commit()

        self.run_sweep(T0 + 6 * MINUTE, "--act")
        printed = self.run_sweep(T0 + 12 * MINUTE, "--act")

        self.assertIn(f"acted: failed run {run_id}, leases released", printed)
        self.assertEqual(
            printed[-1],
            "1 tripped of 1 run swept, 1 failed and leases released")
        self.assertEqual(
            self.conn.execute(
                "SELECT phase, outcome FROM runs WHERE id = ?",
                (run_id,)).fetchone(), ("failed", "failed"))
        self.assertIsNone(
            self.conn.execute(
                "SELECT activeRunId FROM projects").fetchone()[0])
        self.assertIn(branch, self.branches())
        self.assertEqual((wt / "work.txt").read_text(), "half-finished\n")
        # The tripwire provider proves the other half: one swept failure is
        # below the escalation threshold, so nothing was pushed and Linear was
        # never imported, let alone called.

    def test_act_without_sweep_is_refused_rather_than_ignored(self):
        """An operator who typed `--act` meaning to clean up must not get a
        silent no-op, or a loop that claims a ticket."""
        with patch.object(sys, "stderr", io.StringIO()) as complaint:
            with self.assertRaises(SystemExit):
                holophyte.cli.cli(["--act", str(self.target)])

        self.assertIn("--act", complaint.getvalue())

    def test_a_target_with_no_store_is_reported_not_created(self):
        out = io.StringIO()

        with no_network(), patch.object(sys, "stdout", out):
            holophyte.cli.cli(["--sweep", str(self.root / "elsewhere")])

        self.assertIn("no store at", out.getvalue())
        self.assertFalse(holophyte.target.state_dir(self.root / "elsewhere").exists())


def a_dead_pid():
    """A pid the kernel no longer knows: a child that has already been reaped.

    Reuse is possible in principle and negligible in a test's lifetime; the
    alternative, a pid guessed to be free, is a guess.
    """
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


class SuperviseTests(SweepTestCase):
    """`factory.py --supervise <target>`: one watcher per target, on a timer.

    The loop body is driven with the clock as a parameter, like the sweep it
    wraps; the lock is exercised with real files in this test's directory;
    the signal is a real SIGTERM delivered to this process, because a handler
    that is never invoked proves nothing about clean exit.
    """

    def setUp(self):
        super().setUp()
        # `--supervise` posts to the board, so `cli()` needs one named; the
        # provider is a tripwire below, so the values are never sent.
        (self.db.parent / "config.toml").write_text(
            '[board]\nproject_id = "p-1"\nteam = "T"\n')
        self.tgt = holophyte.target.Target.locate(self.target)
        self.lock = holophyte.supervisor.supervisor_lock_path(self.tgt)

    def supervise(self, wait):
        """The mode with an injected sleep, and the provider as a tripwire."""
        out = io.StringIO()
        with patch.dict(sys.modules,
                        {"linear_provider": Tripwire("linear_provider")}):
            with no_network():
                code = holophyte.supervisor.supervise(self.tgt, wait=wait, out=out)
        return code, out.getvalue()

    def heartbeats(self):
        return self.conn.execute(
            "SELECT pid, lastBeat, passes FROM supervisorHeartbeats"
            " ORDER BY startedAt").fetchall()

    def test_a_second_supervisor_exits_nonzero_naming_the_live_pid(self):
        """The first holder is a live child of this process -- a process of
        its own, as a running supervisor is -- and the second start is the
        mode end to end, as an operator (or a service manager retrying)
        would run it."""
        holder = subprocess.Popen(["sleep", "60"])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        holophyte.supervisor.acquire_supervisor_lock(self.lock, self.tgt.path,
                                        pid=holder.pid, now=T0)
        complaint = io.StringIO()

        with patch.object(sys, "stderr", complaint), \
                self.assertRaises(SystemExit) as exited:
            holophyte.cli.cli(["--supervise", str(self.target)])

        self.assertNotEqual(exited.exception.code, 0)
        self.assertIn(f"pid {holder.pid} on {socket.gethostname()}",
                      str(exited.exception))
        # The refusal names the repository as well as the lock: an operator
        # supervising several targets has to know which one is already taken.
        self.assertIn(f"for {self.tgt.path}:", str(exited.exception))
        # And the holder's lock is untouched: a refused starter must not
        # take the file out from under the supervisor it deferred to.
        self.assertEqual(holophyte.supervisor.read_supervisor_lock(self.lock),
                         (holder.pid, T0, socket.gethostname()))
        self.assertEqual(self.lock.read_text().split(),
                         [socket.gethostname(), str(holder.pid), str(T0)])

    def test_a_refused_start_says_whether_the_holder_is_still_beating(self):
        """The refusal is actionable only with the liveness beside it: the
        holder's lock plus a fresh heartbeat is a watcher at work, which is
        the answer to "do I relaunch?" that the lock alone never gave."""
        holder = subprocess.Popen(["sleep", "60"])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        holophyte.supervisor.acquire_supervisor_lock(self.lock, self.tgt.path,
                                        pid=holder.pid, now=T0)
        now = int(holophyte.supervisor.time() * 1000)
        store.record_supervisor_heartbeat(self.conn, holder.pid, T0,
                                          now=now - 12_000)
        self.conn.commit()

        with patch.object(sys, "stderr", io.StringIO()), \
                self.assertRaises(SystemExit) as exited:
            holophyte.cli.cli(["--supervise", str(self.target)])

        message = str(exited.exception)
        self.assertIn(f"pid {holder.pid}", message.splitlines()[0])
        self.assertRegex(
            message.splitlines()[-1],
            rf"^supervisor: live, last heartbeat 1[2-9]s ago"
            rf" \(pid {holder.pid} on {socket.gethostname()}\)$")

    def test_a_lock_naming_no_pid_is_refused_rather_than_guessed_about(self):
        """Never spawn a rival on one ambiguous probe: an empty lock is a
        starter that crashed between its create and its write, or something
        else entirely -- either way not this starter's to remove."""
        self.lock.write_text("")

        with self.assertRaises(holophyte.supervisor.SupervisorHeld) as refused:
            holophyte.supervisor.acquire_supervisor_lock(self.lock, self.tgt.path,
                                            pid=os.getpid(), now=T0)

        self.assertIsNone(refused.exception.pid)
        self.assertIn(str(self.lock), str(refused.exception))
        self.assertTrue(self.lock.exists())

    def test_a_stale_lock_is_reclaimed_and_the_supervisor_runs(self):
        """A dead pid in the lock is a supervisor killed without the chance
        to clean up. The proof of "runs" is a pass on file under this
        process's pid, made while the lock named this process."""
        self.lock.write_text(f"{socket.gethostname()} {a_dead_pid()} {T0}\n")
        held_during_pass = []

        def stop_after_one_pass(_interval):
            held_during_pass.append(
                holophyte.supervisor.read_supervisor_lock(self.lock))
            os.kill(os.getpid(), signal.SIGTERM)

        code, printed = self.supervise(stop_after_one_pass)

        self.assertEqual(code, 0)
        self.assertEqual(held_during_pass[0][0], os.getpid())
        self.assertEqual([(pid, passes) for pid, _at, passes
                          in self.heartbeats()], [(os.getpid(), 1)])
        self.assertIn(f"pid {os.getpid()}", printed)

    def test_two_starters_reclaiming_one_stale_lock_admit_only_one(self):
        """Both starters read the same dead pid; the rival gets its reclaim
        and its new lock in *between* this starter's last look at the stale
        file and its unlink -- the widest window the old inode guard left
        open. The rival's live lock must survive, and this starter must
        lose to it, or two supervisors run side by side."""
        self.lock.write_text(f"{a_dead_pid()} {T0}\n")
        us, rival = os.getpid(), os.getpid() + 1
        real_unlink, real_alive = os.unlink, holophyte.supervisor.pid_alive
        rival_outcome, fired = [], []

        def rival_starts():
            try:
                rival_outcome.append(
                    holophyte.supervisor.acquire_supervisor_lock(
                        self.lock, self.tgt.path, pid=rival, now=T0 + 1))
            except holophyte.supervisor.SupervisorHeld as held:
                rival_outcome.append(held)

        def unlink_with_a_rival_in_the_gap(path, *args, **kwargs):
            if not fired and Path(path) == self.lock:
                fired.append(threading.Thread(target=rival_starts))
                fired[0].start()
                fired[0].join(0.5)
            return real_unlink(path, *args, **kwargs)

        with patch.object(holophyte.supervisor, "pid_alive",
                          lambda pid: pid in (us, rival) or real_alive(pid)), \
                patch.object(os, "unlink", unlink_with_a_rival_in_the_gap):
            try:
                ours = holophyte.supervisor.acquire_supervisor_lock(
                    self.lock, self.tgt.path, pid=us, now=T0)
            except holophyte.supervisor.SupervisorHeld as held:
                ours = held
            fired[0].join(5)

        self.assertTrue(fired, "the rival never got its turn in the gap")
        outcomes = {us: ours, rival: rival_outcome[0]}
        admitted = [who for who, got in outcomes.items()
                    if not isinstance(got, Exception)]
        self.assertEqual(len(admitted), 1, outcomes)
        # The lock on disk names the one starter that was admitted, and the
        # other was refused naming exactly that pid.
        self.assertEqual(holophyte.supervisor.read_supervisor_lock(self.lock)[0],
                         admitted[0])
        refused = outcomes[rival if admitted == [us] else us]
        self.assertEqual(refused.pid, admitted[0])

    def test_sigterm_ends_the_loop_cleanly_and_releases_the_lock(self):
        """A real SIGTERM to this process, delivered while the supervisor is
        between passes: the loop returns rather than raising, the lock is
        gone for the next starter, and the handler this process had before
        is back in place."""
        before = signal.getsignal(signal.SIGTERM)
        passes = []

        def stop_on_second_sleep(_interval):
            passes.append(len(self.heartbeats()))
            if len(passes) == 2:
                os.kill(os.getpid(), signal.SIGTERM)

        code, printed = self.supervise(stop_on_second_sleep)

        self.assertEqual(code, 0)
        self.assertFalse(self.lock.exists())
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        # Two sleeps, two passes, and none after the signal: the flag is read
        # before each pass, so a signal ends the loop at the next check
        # rather than one more sweep later.
        self.assertEqual(self.heartbeats()[0][2], 2)
        self.assertIn("stopping on signal", printed)

    def test_the_loop_body_sweeps_with_action_and_records_a_heartbeat(self):
        """The body is `--sweep --act` plus a beat: a run silent across two
        passes is failed with its lease released, and each pass bumps the
        supervisor's own row to the instant it swept at."""
        run_id = self.a_run()
        self.conn.commit()
        pid = os.getpid()

        with patch.dict(sys.modules,
                        {"linear_provider": Tripwire("linear_provider")}):
            with no_network(), patch.object(sys, "stdout", io.StringIO()):
                holophyte.supervisor.supervise_pass(self.tgt, pid, T0,
                                                    now=T0 + 6 * MINUTE)
                holophyte.supervisor.supervise_pass(self.tgt, pid, T0,
                                                    now=T0 + 12 * MINUTE)

        self.assertEqual(
            self.conn.execute(
                "SELECT phase, outcome FROM runs WHERE id = ?",
                (run_id,)).fetchone(), ("failed", "failed"))
        self.assertIsNone(
            self.conn.execute(
                "SELECT activeRunId FROM projects").fetchone()[0])
        self.assertEqual(self.heartbeats(), [(pid, T0 + 12 * MINUTE, 2)])


class LoopRestartTests(SweepTestCase):
    """A self-merge re-exec the loop did not come back from.

    The loop leaves a `loopRestarts` note before `os.execv()` replaces it; a
    loop that returned claims (a heartbeat) or writes its exit note. Past the
    grace window with neither, the sweep says so -- once -- and is otherwise
    exactly as quiet as it was.
    """

    SHA = "abc1234"
    SECOND = 1000

    def lines(self, now):
        return holophyte.supervisor.sweep_lines(
            holophyte.supervisor.sweep(self.tgt, self.conn, now))

    def restart_lines(self, now):
        return [line for line in self.lines(now) if "re-exec" in line]

    def reported(self):
        return self.conn.execute(
            "SELECT reportedAt FROM loopRestarts ORDER BY id").fetchall()

    def test_a_restart_followed_by_a_heartbeat_is_quiet(self):
        """Restart at T, a claim (which stamps the run's heartbeat) at T+30s,
        sweep at T+200s: nothing about restarts, and the run report reads
        exactly as it did before restarts existed."""
        store.record_loop_restart(self.conn, self.project, self.SHA, now=T0)
        self.a_run(claimed_at=T0 + 30 * self.SECOND)

        lines = self.lines(T0 + 200 * self.SECOND)

        self.assertEqual(lines, ["1 run swept, all healthy"])
        self.assertEqual(self.reported(), [(None,)])

    def test_a_restart_followed_by_the_exit_note_is_quiet(self):
        """A loop that came back to no ready tickets never heartbeats; its
        exit note is what says it returned."""
        store.record_loop_restart(self.conn, self.project, self.SHA, now=T0)
        store.record_loop_return(self.conn, self.project,
                                 now=T0 + 30 * self.SECOND)

        lines = self.lines(T0 + 200 * self.SECOND)

        self.assertEqual(lines, ["no runs in flight, nothing to sweep"])
        self.assertEqual(self.reported(), [(None,)])

    def test_a_restart_nothing_followed_is_reported_once_past_the_grace(self):
        """Restart at T and silence: inside the default two minutes nothing
        is said; at T+200s the line names the sha and the store records the
        condition; a second sweep neither prints nor records it again."""
        store.record_loop_restart(self.conn, self.project, self.SHA, now=T0)

        self.assertEqual(self.restart_lines(T0 + 60 * self.SECOND), [])
        self.assertEqual(self.reported(), [(None,)])

        first = self.lines(T0 + 200 * self.SECOND)

        line, = [line for line in first if "re-exec" in line]
        self.assertIn(f"loop did not return after re-exec from {self.SHA}",
                      line)
        self.assertIn("3.3 min", line)
        # Above the run report, which is the quiet case: a dead loop leaves
        # nothing in flight, and that must not hide the line.
        self.assertEqual(first, [line, "no runs in flight, nothing to sweep"])
        self.assertEqual(self.reported(), [(T0 + 200 * self.SECOND,)])

        second = self.lines(T0 + 260 * self.SECOND)

        self.assertEqual(second, ["no runs in flight, nothing to sweep"])
        self.assertEqual(self.reported(), [(T0 + 200 * self.SECOND,)])

    def test_a_claim_from_before_the_restart_does_not_vouch_for_it(self):
        """The heartbeat that clears a restart has to be newer than it: the
        run the old loop merged just before re-executing is history."""
        run_id = self.a_run(claimed_at=T0 - 10 * MINUTE)
        self.heartbeat_at(run_id, T0 - self.SECOND)
        store.record_loop_restart(self.conn, self.project, self.SHA, now=T0)

        self.assertEqual(len(self.restart_lines(T0 + 200 * self.SECOND)), 1)

    def test_the_supervisor_pass_prints_the_line_on_an_otherwise_quiet_pass(self):
        """`--supervise` prints nothing on a healthy pass; an unreturned
        restart is not a healthy pass."""
        store.record_loop_restart(self.conn, self.project, self.SHA, now=T0)
        self.conn.commit()
        out = io.StringIO()

        with patch.dict(sys.modules,
                        {"linear_provider": Tripwire("linear_provider")}):
            with no_network():
                holophyte.supervisor.supervise_pass(self.tgt, os.getpid(), T0,
                                       now=T0 + 200 * self.SECOND, out=out)
                holophyte.supervisor.supervise_pass(self.tgt, os.getpid(), T0,
                                       now=T0 + 260 * self.SECOND, out=out)

        self.assertEqual(
            out.getvalue().count("loop did not return after re-exec from"
                                 f" {self.SHA}"), 1)


class SupervisorConfigTests(SweepTestCase):
    """`[supervisor]` in the target's `config.toml`: the thresholds have an address.

    An absent table is the constants the tests above were written against; a
    key that is present moves exactly the trip it names; a key outside its
    constraint is refused at startup, before anything is swept.
    """

    def configure(self, text):
        """Write the target's config and build the `Target` that reads it.

        A fresh value rather than the fixture's: a `Target` parses its config
        once, and this test wants the file it just wrote.
        """
        (self.db.parent / "config.toml").write_text(text)
        self.tgt = holophyte.target.Target.locate(self.target)

    def test_an_absent_table_is_the_documented_defaults(self):

        self.assertEqual(holophyte.config.sweep_config(self.tgt),
                         (5 * MINUTE, 2, 1.5, 0.5, 60, 2 * MINUTE))

    def test_heartbeat_stale_min_moves_the_silence_a_trip_needs(self):
        """A heartbeat two and three minutes old on two consecutive sweeps:
        not even a strike under the default five, a trip under one."""
        run_id = self.a_run()
        holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 2 * MINUTE)
        default = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 3 * MINUTE)
        self.assertEqual(default.trips, [])
        self.assertIsNone(self.strikes(run_id))

        self.configure("[supervisor]\nheartbeat_stale_min = 1\n")
        holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 2 * MINUTE)
        result = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 3 * MINUTE)

        trip, = result.trips
        self.assertEqual((trip.run_id, trip.condition),
                         (run_id, holophyte.supervisor.STALE_HEARTBEAT))
        self.assertIn("over 2 consecutive sweeps", trip.evidence)

    def test_stale_strikes_moves_how_many_sightings_a_trip_needs(self):
        run_id = self.a_run()
        self.configure("[supervisor]\nstale_strikes = 3\n")

        holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 6 * MINUTE)
        second = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 12 * MINUTE)
        third = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 18 * MINUTE)

        self.assertEqual(second.trips, [])
        (line,) = second.watched
        self.assertIn("strike 2 of 3", line)
        self.assertEqual([t.run_id for t in third.trips], [run_id])

    def test_unknown_keys_in_the_table_are_left_alone(self):
        self.configure("[supervisor]\nstale_heartbeat_min = 7\n")

        self.assertEqual(holophyte.config.sweep_config(self.tgt).heartbeat_stale_ms,
                         5 * MINUTE)

    def test_a_value_outside_its_constraint_is_refused_at_startup(self):
        """Named key, named constraint, and no sweep: the strike table is
        as empty afterwards as it was before."""
        self.a_run()
        self.conn.commit()
        for line, key, constraint in (
                ("heartbeat_stale_min = -1", "heartbeat_stale_min",
                 "a finite positive number"),
                # TOML spells infinity; `inf > 0` holds, and an infinite
                # threshold never trips while an infinite interval crashes
                # `sleep()` with OverflowError. Both are refused up front.
                ("heartbeat_stale_min = inf", "heartbeat_stale_min",
                 "a finite positive number"),
                ("sweep_interval_sec = inf", "sweep_interval_sec",
                 "a finite positive number"),
                ("budget_grace = nan", "budget_grace",
                 "a finite positive number"),
                ("review_overlap_threshold = 1.5", "review_overlap_threshold",
                 "a number in (0, 1]"),
                ("review_overlap_threshold = 0", "review_overlap_threshold",
                 "a number in (0, 1]"),
                ("stale_strikes = 1.5", "stale_strikes",
                 "a positive integer"),
                ("budget_grace = true", "budget_grace",
                 "a finite positive number"),
                ('sweep_interval_sec = "60"', "sweep_interval_sec",
                 "a finite positive number"),
                ("restart_grace_sec = 0", "restart_grace_sec",
                 "a finite positive number")):
            with self.subTest(line=line):
                self.configure(f"[supervisor]\n{line}\n")
                with self.assertRaises(SystemExit) as raised, \
                        patch.object(holophyte.supervisor, "time",
                                     lambda: (T0 + 6 * MINUTE) / 1000):
                    holophyte.cli.cli(["--sweep", str(self.target)])
                message = str(raised.exception)
                self.assertIn(f"[supervisor] {key}", message)
                self.assertIn(constraint, message)
                self.assertIn(str(self.db.parent / "config.toml"), message)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM sweepStrikes").fetchone(),
            (0,))

    def test_a_non_table_supervisor_value_is_refused_even_when_falsy(self):
        """`supervisor = false` is not "no table": it is a wrong-typed key,
        and gets the same startup refusal a string or list would."""
        for line, kind in (("supervisor = false", "bool"),
                           ("supervisor = 0", "int"),
                           ('supervisor = ""', "str"),
                           ("supervisor = []", "list"),
                           ('supervisor = "table"', "str")):
            with self.subTest(line=line):
                self.configure(line + "\n")
                with self.assertRaises(SystemExit) as raised:
                    holophyte.config.sweep_config(self.tgt)
                message = str(raised.exception)
                self.assertIn("[supervisor] must be a table", message)
                self.assertIn(f"got {kind}", message)

    def test_restart_grace_sec_moves_how_long_a_re_exec_may_take(self):
        """A restart 200s old: reported under the default two minutes, not
        under a five-minute grace."""
        store.record_loop_restart(self.conn, self.project, "abc1234", now=T0)
        self.configure("[supervisor]\nrestart_grace_sec = 300\n")

        patient = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 200_000)
        self.assertEqual(patient.restarts, ())

        self.configure("[supervisor]\nrestart_grace_sec = 120\n")
        default = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 200_000)
        self.assertEqual(default.restarts, (("abc1234", 200_000),))

    def test_sweep_interval_sec_is_the_supervisor_s_sleep(self):
        self.configure("[supervisor]\nsweep_interval_sec = 7\n")
        slept = []

        def stop_after_one(interval):
            slept.append(interval)
            os.kill(os.getpid(), signal.SIGTERM)

        out = io.StringIO()
        with patch.dict(sys.modules,
                        {"linear_provider": Tripwire("linear_provider")}):
            with no_network():
                code = holophyte.supervisor.supervise(self.tgt, wait=stop_after_one,
                                                      out=out)

        self.assertEqual(code, 0)
        self.assertEqual(slept, [7])
        self.assertIn("every 7s", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class HeartbeatWhileTests(SweepTestCase):
    """`heartbeat_while()` keeps a waiting loop off the sweep's stale list.

    Real clock, unlike the rest of this file: the beat is a timer thread, so
    the sweeps here are given the wall-clock `now` the beats are stamped
    against, and the thresholds are set in tens of milliseconds to match.
    """

    def knobs(self, stale_ms):
        return holophyte.config.sweep_config(self.tgt)._replace(
            heartbeat_stale_ms=stale_ms)

    def sweep_now(self, knobs):
        return holophyte.supervisor.sweep(
            self.tgt, self.conn, int(time.time() * 1000), knobs=knobs)

    def test_a_loop_that_beats_is_alive_and_one_that_stopped_is_dead(self):
        """Inside the block the run outlives the stale threshold untripped;
        after it the beats stop and the same threshold trips it on the
        second sighting, as a dead loop's run has always been."""
        run_id = self.a_run(claimed_at=int(time.time() * 1000))
        knobs = self.knobs(stale_ms=300)
        stale_span = knobs.heartbeat_stale_ms * knobs.stale_strikes / 1000

        with holophyte.runs.heartbeat_while(self.conn, run_id, 0.05):
            phase_before = store.run_phase(self.conn, run_id)
            time.sleep(stale_span)
            alive = self.sweep_now(knobs)
            time.sleep(stale_span)
            still_alive = self.sweep_now(knobs)

        self.assertEqual(alive.trips, [])
        self.assertEqual(still_alive.trips, [])
        self.assertIsNone(self.strikes(run_id))
        # The beat moved the heartbeat without touching the phase or the
        # narrative: no `phase_change` beyond the one `a_run()` made.
        self.assertEqual(store.run_phase(self.conn, run_id), phase_before)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM runEvents WHERE runId = ?",
            (run_id,)).fetchone()[0], 1)

        time.sleep(stale_span)
        first = self.sweep_now(knobs)
        first_strikes = self.strikes(run_id)[0]
        time.sleep(stale_span)
        second = self.sweep_now(knobs)

        self.assertEqual((first.trips, first_strikes), ([], 1))
        self.assertEqual([(t.run_id, t.condition) for t in second.trips],
                         [(run_id, "stale_heartbeat")])

    def test_a_heartbeat_on_an_ended_run_changes_nothing(self):
        run_id = self.a_run()
        store.release(self.conn, run_id, "merged", now=T0 + MINUTE)
        before = self.conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

        moved = store.heartbeat(self.conn, run_id, now=T0 + 5 * MINUTE)

        self.assertFalse(moved)
        after = self.conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        self.assertEqual(after, before)


class HostLabelTests(SweepTestCase):
    """`[report] host_label` on the sweep table and the supervisor's lines.

    The lock file and the store keep the real hostname -- `reclaim_turn` and
    `still_tripped` compare against it -- and only the printed lines change.
    """

    LABEL = "writer-1"

    def setUp(self):
        super().setUp()
        (self.db.parent / "config.toml").write_text(
            f'[report]\nhost_label = "{self.LABEL}"\n'
            '[board]\nproject_id = "p-1"\nteam = "T"\n')
        self.tgt = holophyte.target.Target.locate(self.target)
        self.lock = holophyte.supervisor.supervisor_lock_path(self.tgt)

    def test_the_sweep_table_and_watched_line_show_the_label(self):
        self.a_run()
        self.conn.commit()

        first = self.run_sweep(T0 + 6 * MINUTE)
        printed = self.run_sweep(T0 + 12 * MINUTE)

        hostname = socket.gethostname()
        self.assertNotIn(hostname, "\n".join(first + printed))
        self.assertTrue(first[1].endswith(f" on {self.LABEL}"), first[1])
        self.assertEqual(printed[1].split()[-1], self.LABEL)

    def test_the_startup_and_refusal_lines_show_the_label_over_a_real_lock(self):
        out = io.StringIO()
        with patch.dict(sys.modules,
                        {"linear_provider": Tripwire("linear_provider")}):
            with no_network(), \
                    patch.object(holophyte.supervisor, "supervise_pass"):
                holophyte.supervisor.supervise(
                    self.tgt, wait=lambda _i: os.kill(os.getpid(), signal.SIGTERM),
                    out=out)
        holder = subprocess.Popen(["sleep", "60"])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        holophyte.supervisor.acquire_supervisor_lock(
            self.lock, self.tgt.path, pid=holder.pid, now=T0)

        with patch.object(sys, "stderr", io.StringIO()), \
                self.assertRaises(SystemExit) as exited:
            holophyte.cli.cli(["--supervise", str(self.target)])

        hostname = socket.gethostname()
        self.assertIn(f"as pid {os.getpid()} on {self.LABEL}", out.getvalue())
        self.assertNotIn(hostname, out.getvalue())
        self.assertIn(f"pid {holder.pid} on {self.LABEL}", str(exited.exception))
        self.assertNotIn(hostname, str(exited.exception))
        # The lock itself still names the machine: another host reading the
        # state directory has to know whose pid that is.
        self.assertEqual(self.lock.read_text().split()[0], hostname)
