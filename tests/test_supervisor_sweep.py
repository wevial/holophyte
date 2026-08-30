"""Supervisor contract: the read-only stale-run sweep.

The loop watches itself only while it is alive; a crashed or hung run leaves a
row in a work phase, a heartbeat that stopped and a lease nobody gives back.
These tests assert what the sweep notices about such a run and, just as
importantly, what it refuses to notice: one silent sighting is not evidence, a
run inside its budget is not late, and a finished or parked run is not swept at
all. The clock is a parameter throughout, so every age below is arithmetic
rather than a sleep.

Run: python3 -m unittest discover -s tests -p 'test_supervisor*' -v
"""
from __future__ import annotations

import importlib.util
import io
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
SPEC = importlib.util.spec_from_file_location("holophyte_factory", ROOT / "factory.py")
factory = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(factory)

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
        self.db = self.root / "repo.holophyte.db"
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project = store.ensure_project(self.conn, "team-1", self.target)
        self.tickets = 0

    def a_run(self, budget_min=25, claimed_at=T0, phase="working"):
        """One live run of its own ticket, claimed at `claimed_at`.

        `claim()` stamps `startedAt` and `lastHeartbeat` together, so a fresh
        run's heartbeat is its claim time until a stage boundary moves it;
        `heartbeat_at()` below is how a test moves it.
        """
        self.tickets += 1
        n = self.tickets
        ticket = store.mirror_ticket(
            self.conn, self.project, linear_issue_id=f"issue-{n}",
            linear_identifier=f"KO-{n}", title=f"ticket {n}",
            time_box_ms=budget_min and budget_min * MINUTE)
        run_id = store.claim(self.conn, self.project, ticket, now=claimed_at)
        if phase != "claimed":
            store.set_phase(self.conn, run_id, phase, now=claimed_at)
        return run_id

    def heartbeat_at(self, run_id, at):
        """Move the run's last heartbeat, the way a stage boundary would."""
        store.set_phase(self.conn, run_id, store.run_phase(self.conn, run_id),
                        now=at)

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

        result = factory.sweep(self.conn, T0 + 2 * MINUTE)

        self.assertEqual(result.trips, [])
        self.assertEqual(result.swept, 1)
        self.assertIsNone(self.strikes(run_id))

    def test_one_stale_sighting_is_a_strike_and_not_a_trip(self):
        """The two-strike rule's whole point: a load spike is not a death."""
        run_id = self.a_run()

        result = factory.sweep(self.conn, T0 + 6 * MINUTE)

        self.assertEqual(result.trips, [])
        self.assertEqual(self.strikes(run_id), (1, T0 + 6 * MINUTE))

    def test_two_consecutive_stale_sightings_trip_the_run(self):
        run_id = self.a_run(phase="reviewing")

        factory.sweep(self.conn, T0 + 6 * MINUTE)
        result = factory.sweep(self.conn, T0 + 7 * MINUTE)

        trip, = result.trips
        self.assertEqual(
            (trip.run_id, trip.ticket, trip.phase, trip.condition),
            (run_id, "KO-1", "reviewing", "stale_heartbeat"))
        # The age the verdict was reached on, so a reader can agree with it:
        # seven minutes of silence, seen twice.
        self.assertIn("7.0 min", trip.evidence)
        self.assertIn("2 consecutive sweeps", trip.evidence)

    def test_a_heartbeat_between_sweeps_clears_the_count(self):
        """Consecutive, not cumulative: a run that answers starts over."""
        run_id = self.a_run()

        factory.sweep(self.conn, T0 + 6 * MINUTE)
        self.heartbeat_at(run_id, T0 + 7 * MINUTE)
        alive = factory.sweep(self.conn, T0 + 8 * MINUTE)
        stale_again = factory.sweep(self.conn, T0 + 14 * MINUTE)

        self.assertEqual(alive.trips, [])
        # Silent again six minutes later, and back to a first strike rather
        # than the second one that would have tripped it.
        self.assertEqual(stale_again.trips, [])
        self.assertEqual(self.strikes(run_id), (1, T0 + 14 * MINUTE))


class TimeBoxTests(SweepTestCase):
    """The budget trip: wall clock since the claim, against the run's box."""

    def test_a_run_past_the_grace_multiple_of_its_budget_trips(self):
        run_id = self.a_run(budget_min=20)
        at = T0 + 31 * MINUTE  # 1.55x of 20 min
        self.heartbeat_at(run_id, at)  # alive, and still overdue

        trip, = factory.sweep(self.conn, at).trips

        self.assertEqual((trip.run_id, trip.condition), (run_id, "time_box"))
        self.assertIn("31.0 min", trip.evidence)
        self.assertIn("20 min box", trip.evidence)

    def test_a_run_inside_the_grace_multiple_does_not_trip(self):
        """Over its estimate is not overdue: the grace is 1.5x, not 1x."""
        run_id = self.a_run(budget_min=20)
        at = T0 + 29 * MINUTE  # 1.45x of 20 min
        self.heartbeat_at(run_id, at)

        self.assertEqual(factory.sweep(self.conn, at).trips, [])

    def test_a_run_claimed_against_no_estimate_has_no_box_to_blow(self):
        run_id = self.a_run(budget_min=None)
        at = T0 + 600 * MINUTE
        self.heartbeat_at(run_id, at)

        self.assertEqual(factory.sweep(self.conn, at).trips, [])


class NotSweptTests(SweepTestCase):
    """Rows the sweep must leave alone, however old their heartbeats are."""

    def test_an_ended_run_is_not_swept(self):
        """A finished run's heartbeat stopped because the work stopped."""
        done = self.a_run()
        store.release(self.conn, done, "merged", now=T0 + MINUTE)
        live = self.a_run(claimed_at=T0 + 2 * MINUTE)

        result = factory.sweep(self.conn, T0 + 20 * MINUTE)

        self.assertEqual(result.swept, 1)
        self.assertEqual([trip.run_id for trip in result.trips], [])
        self.assertIsNone(self.strikes(done))
        self.assertEqual(self.strikes(live)[0], 1)

    def test_a_run_parked_for_an_operator_is_not_swept(self):
        """It has no heartbeat by design: it is waiting for a human answer."""
        parked = self.a_run(phase="blocked_on_operator")

        first = factory.sweep(self.conn, T0 + 6 * MINUTE)
        second = factory.sweep(self.conn, T0 + 12 * MINUTE)

        self.assertEqual((first.swept, second.swept), (0, 0))
        self.assertEqual(second.trips, [])
        self.assertIsNone(self.strikes(parked))


class SweepModeTests(SweepTestCase):
    """`factory.py --sweep <target>` as an operator runs it."""

    def setUp(self):
        super().setUp()
        # `cli()` retargets the module for real, so what it overwrites is put
        # back rather than left pointing at this test's temporary directory.
        original = {name: getattr(factory, name)
                    for name in ("TARGET", "STORE_PATH", "WORKTREES")}

        def restore():
            for name, value in original.items():
                setattr(factory, name, value)

        self.addCleanup(restore)

    def run_sweep(self, at):
        """The mode end to end, with the provider and the network as tripwires.

        The mode reads the wall clock, which is the one thing about it a test
        cannot arrange, so `time` is what `at` replaces -- the seam the sweep
        itself takes as a parameter.
        """
        out = io.StringIO()
        with patch.dict(sys.modules,
                        {"linear_provider": Tripwire("linear_provider")}):
            with no_network(), patch.object(sys, "stdout", out), \
                    patch.object(factory, "time", lambda: at / 1000):
                factory.cli(["--sweep", str(self.target)])
        return out.getvalue().splitlines()

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
        printed = self.run_sweep(T0 + 7 * MINUTE)

        self.assertEqual(first, ["1 run swept, all healthy"])
        self.assertEqual(printed[0].split(), list(factory.SWEEP_HEADERS))
        self.assertEqual(printed[1].split()[:5],
                         ["KO-1", "run", str(run_id), "working",
                          "stale_heartbeat"])
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

    def test_a_target_with_no_store_is_reported_not_created(self):
        out = io.StringIO()

        with no_network(), patch.object(sys, "stdout", out):
            factory.cli(["--sweep", str(self.root / "elsewhere")])

        self.assertIn("no store at", out.getvalue())
        self.assertFalse((self.root / "elsewhere.holophyte.db").exists())


if __name__ == "__main__":
    unittest.main()
