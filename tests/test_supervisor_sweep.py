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
import socket
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
import holophyte.cli  # noqa: E402 - after the sys.path insert above
import holophyte.config  # noqa: E402 - after the sys.path insert above
import holophyte.project  # noqa: E402 - after the sys.path insert above
import holophyte.supervisor  # noqa: E402 - after the sys.path insert above
import holophyte.sweep_report  # noqa: E402 - after the sys.path insert above
import review_runner  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above

sys.path.insert(0, str(Path(__file__).resolve().parent))  # the runner's shim
from sweep_fixture import (  # noqa: E402 - after the insert
    MINUTE,
    T0,
    StubProvider,
    SweepTestCase,
    no_network,
)
from test_review_runner import docker_shim  # noqa: E402 - after the insert


class WorktreeDebrisTests(SweepTestCase):
    def test_only_final_ticket_worktrees_are_reported_without_removal(self):
        def git(*args):
            subprocess.run(["git", *args], cwd=self.target, check=True,
                           capture_output=True)
        git("init", "-b", "main")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "--allow-empty", "-m", "base")
        ended = self.a_run()
        store.set_branch(self.conn, ended, "holo/ko-1")
        store.release(self.conn, ended, "failed", "cancelled")
        store.tickets.walk_ticket(self.conn, self.ticket_of[ended], "abandoned")
        live = self.a_run()
        store.set_branch(self.conn, live, "holo/ko-2")
        paths = [self.tgt.worktrees / f"ko-{n}" for n in (1, 2)]
        for n, path in enumerate(paths, 1):
            git("worktree", "add", "-b", f"holo/ko-{n}", str(path))
        # Existing stores can retain a symlink spelling of the same repository.
        alias = self.root / "alias"
        alias.symlink_to(self.target, target_is_directory=True)
        self.conn.execute("UPDATE projects SET repoPath = ? WHERE id = ?",
                          (str(alias), self.project))
        self.conn.commit()
        for flags in ((), ("--act",)):
            printed = "\n".join(self.run_sweep(T0, *flags))
            self.assertIn(f"debris: KO-1 (abandoned): {paths[0]}", printed)
            self.assertNotIn(str(paths[1]), printed)
            self.assertTrue(all(path.is_dir() for path in paths))


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
        printed = holophyte.sweep_report.sweep_lines(result)
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
        run_id = self.a_run(active_work=True, budget_min=20)
        at = T0 + 31 * MINUTE  # 1.55x of 20 min
        self.heartbeat_at(run_id, at)  # alive, and still overdue

        trip, = holophyte.supervisor.sweep(self.tgt, self.conn, at).trips

        self.assertEqual((trip.run_id, trip.condition), (run_id, "time_box"))
        self.assertIn("31.0 min", trip.evidence)
        self.assertIn("20 min box", trip.evidence)

    def test_a_run_inside_the_grace_multiple_does_not_trip(self):
        """Over its estimate is not overdue: the grace is 1.5x, not 1x."""
        run_id = self.a_run(active_work=True, budget_min=20)
        at = T0 + 29 * MINUTE  # 1.45x of 20 min
        self.heartbeat_at(run_id, at)

        self.assertEqual(holophyte.supervisor.sweep(self.tgt, self.conn, at).trips, [])

    def test_a_run_claimed_against_no_estimate_has_no_box_to_blow(self):
        run_id = self.a_run(active_work=True, budget_min=None)
        at = T0 + 600 * MINUTE
        self.heartbeat_at(run_id, at)

        self.assertEqual(holophyte.supervisor.sweep(self.tgt, self.conn, at).trips, [])

    def test_a_scaled_run_inside_its_scaled_box_does_not_trip(self):
        """`[agents] budget_scale` stretches the box the run is counted
        against: past the bare estimate's grace but inside the scaled
        box is a slower harness, not a blown budget."""
        self.configure("[agents]\nbudget_scale = 2\n")
        run_id = self.a_run(active_work=True, budget_min=30)
        at = T0 + 50 * MINUTE  # past 30 min x 1.5 grace; inside 60 x 1.5
        self.heartbeat_at(run_id, at)  # alive, so only the box could trip it

        self.assertEqual(
            holophyte.supervisor.sweep(self.tgt, self.conn, at).trips, [])

    def test_a_scaled_run_past_its_scaled_box_still_trips(self):
        """The scale is not an escape: past the scaled box's grace the
        trip fires, and the evidence names the box it was counted on."""
        self.configure("[agents]\nbudget_scale = 2\n")
        run_id = self.a_run(active_work=True, budget_min=30)
        at = T0 + 91 * MINUTE  # past the 60 min box at 1.5 grace
        self.heartbeat_at(run_id, at)

        trip, = holophyte.supervisor.sweep(self.tgt, self.conn, at).trips

        self.assertEqual((trip.run_id, trip.condition), (run_id, "time_box"))
        self.assertIn("60 min box", trip.evidence)

    def test_a_run_past_the_run_cap_trips_despite_earned_rounds(self):
        """KO-388's gap, closed: two recorded rounds earn the run
        30 x 3 x 1.5 = 135 min under the per-turn formula, but `run_cap`
        3 cuts the allowance to 90 -- so a run 100 minutes in trips for
        the box it blew where the per-turn formula alone would not."""
        run_id = self.a_run(active_work=True, budget_min=30, phase="addressing")
        for number in (1, 2):
            store.record_review_round(
                self.conn, run_id, number, "changes_requested", "reviewer",
                findings=[{"path": "a.py", "line": number, "severity": "p1",
                           "message": f"fix a.py ({number})"}],
                started_at=T0 + number * MINUTE,
                ended_at=T0 + number * MINUTE + 1)
        at = T0 + 100 * MINUTE  # under 135 (rounds x grace); past 90 (run cap)
        self.heartbeat_at(run_id, at)  # alive, so only the box could trip it

        trip, = holophyte.supervisor.sweep(self.tgt, self.conn, at).trips

        self.assertEqual((trip.run_id, trip.condition), (run_id, "time_box"))
        self.assertIn("100.0 min", trip.evidence)
        self.assertIn("3.0x run cap", trip.evidence)


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
        for phase in ("verifying", "reviewing", "merge_gate", "merging"):
            store.set_phase(self.conn, done, phase, now=T0)
        store.release(self.conn, done, "merged", now=T0 + MINUTE)
        live = self.a_run(claimed_at=T0 + 2 * MINUTE)

        result = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 20 * MINUTE)

        self.assertEqual(result.swept, 1)
        self.assertEqual([trip.run_id for trip in result.trips], [])
        self.assertIsNone(self.strikes(done))
        self.assertEqual(self.strikes(live)[0], 1)

    def test_a_run_parked_for_an_operator_is_not_swept(self):
        """It has no heartbeat by design: it is waiting for a human answer,
        or (`[merge] approve = "human"`) for a human to say merge."""
        for phase in ("blocked_on_operator", "awaiting_merge_approval"):
            with self.subTest(phase=phase):
                self.setUp()
                parked = self.a_run(phase=phase)

                first = holophyte.supervisor.sweep(self.tgt, self.conn,
                                                   T0 + 6 * MINUTE)
                second = holophyte.supervisor.sweep(self.tgt, self.conn,
                                                    T0 + 12 * MINUTE)

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
        """The project column (never written now), the ticket's lease, and
        where the ticket's pointer went."""
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
        ends as a failure naming what tripped it, the ticket stops holding a
        lease for a process that is gone, and the ticket keeps a pointer to
        the run that failed on it."""
        run_id = self.a_run()
        at = self.trip()

        result = self.act(at)

        self.assertEqual(len(result.trips), 1)
        self.assertEqual(self.conn.execute(
            "SELECT failureKind FROM runs WHERE id = ?", (run_id,)).fetchone(),
            ("swept",))
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
        rendered window, in a target that renders one, names the condition
        beside the run it ended."""
        self.tgt.config_path.write_text('[report]\nfindings = "repo"\n')
        self.tgt = holophyte.project.Project.locate(self.target)
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
        self.assertEqual(self.leases(run_id), (None, run_id, None))
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
            for phase in ("verifying", "reviewing", "merge_gate", "merging"):
                store.set_phase(conn, trip.run_id, phase, now=at)
            store.release(conn, trip.run_id, "merged", now=at)
            return original_act(target, conn, trip, provider, knobs)

        with patch.object(holophyte.supervisor, "act_on_trip", finish_then_act):
            result = self.act(at)
        lines = holophyte.sweep_report.sweep_lines(result)

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
                for phase in ("verifying", "reviewing", "merge_gate", "merging"):
                    store.set_phase(conn, trip.run_id, phase, now=at)
                store.release(conn, trip.run_id, "merged", now=at)
            return original_act(target, conn, trip, provider, knobs)

        with patch.object(holophyte.supervisor, "act_on_trip", finish_one_then_act):
            lines = holophyte.sweep_report.sweep_lines(self.act(at))

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

        self.assertEqual(printed, ["review containers: skipped (no docker on PATH)",
                                   "1 run swept, all healthy"])

    def test_a_tripped_run_is_printed_and_nothing_is_claimed(self):
        run_id = self.a_run()
        self.conn.commit()

        first = self.run_sweep(T0 + 6 * MINUTE)
        printed = self.run_sweep(T0 + 12 * MINUTE)

        # Each pass opens with the review-container section, one line here.
        self.assertEqual(first[1], "1 run swept, none tripped")
        self.assertIn("strike 1 of 2", first[2])
        self.assertEqual(printed[1].split(), list(holophyte.sweep_report.SWEEP_HEADERS))
        self.assertEqual(printed[2].split()[:5],
                         ["KO-1", "run", str(run_id), "working",
                          "stale_heartbeat"])
        # A store read from another machine has to say where the run is:
        # the trip line and the watched line both end in the host.
        self.assertEqual(printed[2].split()[-1], socket.gethostname())
        self.assertTrue(first[2].endswith(f" on {socket.gethostname()}"),
                        first[2])
        self.assertEqual(printed[-1], "1 tripped of 1 run swept")
        # Read-only apart from the strikes: the run is still in flight, in the
        # phase it stopped in, and the lease it holds was not given back.
        self.assertEqual(
            self.conn.execute(
                "SELECT phase, endedAt, outcome FROM runs WHERE id = ?",
                (run_id,)).fetchone(), ("working", None, None))
        self.assertEqual(
            self.conn.execute(
                "SELECT activeRunId FROM tickets").fetchone()[0], run_id)

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
        self.assertFalse(holophyte.project.state_dir(self.root / "elsewhere").exists())


class ReviewContainerSweepTests(SweepTestCase):
    """The `review containers` section of `--sweep`: a leaked reviewer is
    listed, removed only under `--act`, and a live one is left alone.

    Docker is a shim on PATH that records its argv (`docker_shim()` in the
    runner's tests), so what is asserted is what the sweep asked of it.
    """

    def setUp(self):
        super().setUp()
        self.bin_dir, self.env = docker_shim(self.root / "shim")
        self.scratch = self.root / "reviews"
        (self.scratch / "review.live1234").mkdir(parents=True)
        Path(self.env["HOLOPHYTE_DOCKER_PS"]).write_text(
            "holophyte-review-live1234\nholophyte-review-gone5678\n")
        self.log = Path(self.env["HOLOPHYTE_DOCKER_LOG"])

    def recorded(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def report(self, act=False, env=None):
        out = io.StringIO()
        with patch.dict(os.environ, env if env is not None else self.env), \
                patch.object(review_runner, "SCRATCH_ROOT", self.scratch):
            status = holophyte.sweep_report.sweep_report(
                self.tgt, self.conn, T0, out=out, act=act)
        return status, out.getvalue()

    def test_a_read_only_sweep_names_the_stray_and_removes_nothing(self):
        status, printed = self.report()

        self.assertIsNone(status)
        section = printed[printed.index("review containers"):]
        self.assertIn("stray holophyte-review-gone5678", section)
        self.assertNotIn("live1234", section)
        self.assertFalse([line for line in self.recorded()
                          if line.startswith("rm ")], self.recorded())

    def test_an_acting_sweep_removes_the_stray_and_says_so(self):
        status, printed = self.report(act=True)

        self.assertIsNone(status)
        section = printed[printed.index("review containers"):]
        self.assertIn("removed stray holophyte-review-gone5678", section)
        self.assertNotIn("live1234", section)
        self.assertEqual([line for line in self.recorded()
                          if line.startswith("rm ")],
                         ["rm --force holophyte-review-gone5678"])

    def test_without_docker_the_section_says_skipped_and_the_status_holds(
            self):
        empty = self.root / "empty"
        empty.mkdir()
        status, printed = self.report(env={"PATH": str(empty)})

        self.assertIsNone(status)
        section = printed[printed.index("review containers"):]
        self.assertIn("skipped", section)
        self.assertIn("docker", section)
        self.assertFalse(self.log.exists())


class HeldProjectTests(SweepTestCase):
    def test_hold_preserves_working_and_parked_runs_across_supervisor_pass(self):
        live = self.a_run()
        parked = self.a_run()
        for phase in ("verifying", "reviewing", "merge_gate"):
            store.set_phase(self.conn, parked, phase, now=T0)
        store.tickets.transition(self.conn, self.ticket_of[parked],
                                 "blocked_on_operator")
        store.park(self.conn, parked, "awaiting_merge_approval", now=T0,
                   pr_url="https://github.com/org/repo/pull/1")
        before = self.conn.execute("SELECT * FROM runs ORDER BY id").fetchall()
        store.hold(self.conn, self.project, "reboot pending")
        provider = Mock(team="team-1")
        with patch("holophyte.reconcile._reconcile_pull_requests") as reconcile:
            holophyte.supervisor.supervise_pass(
                self.tgt, 42, T0, now=T0 + MINUTE, provider=provider,
                out=io.StringIO()
            )
        reconcile.assert_not_called()
        self.assertEqual(provider.mock_calls, [])
        self.assertEqual(
            self.conn.execute("SELECT * FROM runs ORDER BY id").fetchall(), before
        )
        store.set_phase(self.conn, live, "verifying", now=T0 + MINUTE)
        self.assertEqual(store.run_phase(self.conn, live), "verifying")
        output = "\n".join(self.run_sweep(T0 + MINUTE))
        self.assertIn("held: reboot pending", output)
        self.assertIn(str(self.target), output)

if __name__ == "__main__":
    unittest.main()
