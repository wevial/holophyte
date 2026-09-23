"""The supervisor's time box counts the run's turns (KO-340).

The loop gives every implementer turn -- the first, and the fix after each
review round -- the ticket's whole budget as its own cap, so the sweep's box
scales with the review rounds the run has recorded, bounded by its review cap.
Runs 160 and 161 were swept at 46 min inside a legitimate fix round because the
sweep read the box once for the whole run.
"""

from __future__ import annotations

import contextlib
import io
import signal
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_fixture import MINUTE, T0, SweepTestCase  # noqa: E402

import holophyte.gates  # noqa: E402 - after the sys.path insert above
import holophyte.supervisor  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above


class TimeBoxAllowanceTests(unittest.TestCase):
    """The arithmetic, witnessed without a store: 30 min, grace 1.5."""

    def test_the_box_is_counted_once_per_turn_up_to_the_cap(self):
        allowance = holophyte.supervisor.time_box_allowance
        # A run_cap high enough not to bind keeps the per-turn arithmetic.
        self.assertEqual(allowance(30, 0, 2, 1.5, 5), 45)
        self.assertEqual(allowance(30, 1, 2, 1.5, 5), 90)
        self.assertEqual(allowance(30, 3, 2, 1.5, 5), 135)

    def test_the_run_cap_bounds_the_allowance_whatever_the_rounds(self):
        """KO-416: three rounds would earn 135 min at grace 1.5; the
        default run cap of three boxes cuts the allowance to 90."""
        allowance = holophyte.supervisor.time_box_allowance
        self.assertEqual(allowance(30, 3, 2, 1.5, 3.0), 90)
        self.assertEqual(allowance(30, 0, 2, 1.5, 3.0), 45)


class TimeBoxPerTurnSweepTests(SweepTestCase):
    """A run 46 min old with a 30 min box: overdue on one turn, not on two."""

    def test_disabled_supervisor_exits_before_lock_or_sweep(self):
        store.set_admission(self.conn, 1, "disabled", "retired")
        out = io.StringIO()
        with patch.object(holophyte.supervisor, "acquire_supervisor_lock") as lock:
            holophyte.supervisor.supervise(self.tgt, out=out)
            with (contextlib.chdir(self.target),
                  patch.object(self.tgt, "path", Path("."))):
                holophyte.supervisor.supervise(self.tgt, out=out)
        lock.assert_not_called()
        self.assertIn("disabled: retired", out.getvalue())

    def a_round(self, run_id, number=1, at=T0 + 20 * MINUTE):
        store.record_review_round(
            self.conn, run_id, number, "changes_requested", "reviewer",
            findings=[{"path": "a.py", "line": 1, "severity": "p1",
                       "message": "fix a.py"}],
            started_at=at, ended_at=at + MINUTE)

    def sweep_at_46(self, run_id):
        at = T0 + 46 * MINUTE
        self.heartbeat_at(run_id, at)
        return holophyte.supervisor.sweep(self.tgt, self.conn, at).trips

    def test_a_fix_round_after_a_review_is_not_swept_as_overtime(self):
        run_id = self.a_run(active_work=True, budget_min=30, phase="addressing")
        store.set_review_round_cap(self.conn, run_id, 2)
        self.a_round(run_id)

        self.assertEqual(self.sweep_at_46(run_id), [])

    def test_the_same_run_with_no_round_trips_on_its_single_box(self):
        run_id = self.a_run(active_work=True, budget_min=30, phase="working")
        store.set_review_round_cap(self.conn, run_id, 2)

        trip, = self.sweep_at_46(run_id)

        self.assertEqual((trip.run_id, trip.condition), (run_id, "time_box"))
        self.assertIn("46.0 min of agent work against a 30 min box × 1 turn",
                      trip.evidence)
        self.assertNotIn("2 turns", trip.evidence)

    def test_a_null_cap_is_bounded_by_the_loops_default(self):
        """No `reviewRoundCap` yet: the default cap bounds the multiplier,
        so three rounds are worth the default's turns and nothing raises."""
        run_id = self.a_run(active_work=True, budget_min=30, phase="addressing")
        for number in (1, 2, 3):
            self.a_round(run_id, number, at=T0 + (10 + 5 * number) * MINUTE)
        self.assertIsNone(self.conn.execute(
            "SELECT reviewRoundCap FROM runs WHERE id = ?",
            (run_id,)).fetchone()[0])

        at = T0 + 140 * MINUTE  # past 3 turns × 30 × 1.5 = 135, inside 4 turns
        self.heartbeat_at(run_id, at)
        trip, = holophyte.supervisor.sweep(self.tgt, self.conn, at).trips

        self.assertEqual(trip.condition, "time_box")
        self.assertIn(f"× {1 + holophyte.supervisor.MAX_ROUNDS} turns",
                      trip.evidence)


class MergeLockSweepTests(SweepTestCase):
    """KO-342: the sweep clears a merge lock whose run has ended, names it,
    and leaves a live run's lock alone."""

    def setUp(self):
        super().setUp()
        # run_sweep hides executable tools; CLI startup still needs an identity.
        build = patch("holophyte.startup.build_sha", return_value="test-build")
        build.start()
        self.addCleanup(build.stop)

    def lock_for(self, run_id):
        path = holophyte.gates.merge_lock_path(self.tgt)
        path.write_text(f"{run_id} {T0 / 1000:.3f}\n")
        return path

    def test_an_acting_sweep_removes_the_lock_of_an_ended_run(self):
        run_id = self.a_run(phase="merge_gate")
        store.release(self.conn, run_id, "failed", "died at the gate",
                      now=T0 + MINUTE)
        path = self.lock_for(run_id)

        quiet = self.run_sweep(T0 + 2 * MINUTE)
        self.assertTrue(path.exists())
        self.assertIn(f"stale merge lock: run {run_id} ended;"
                      " --sweep --act removes it", quiet)

        acted = self.run_sweep(T0 + 2 * MINUTE, "--act")

        self.assertFalse(path.exists())
        self.assertIn(f"removed stale merge lock: run {run_id} ended", acted)

    def test_a_live_runs_lock_is_kept_and_reported(self):
        run_id = self.a_run(phase="merge_gate")
        path = self.lock_for(run_id)

        lines = self.run_sweep(T0 + MINUTE, "--act")

        self.assertTrue(path.exists())
        self.assertTrue(any(line.startswith(
            f"merge lock held by run {run_id} (merge_gate)") for line in lines),
            lines)

    def test_a_lock_a_live_process_holds_survives_the_sweep_of_its_ended_run(self):
        """The store says the run ended, but the process that took the lock
        is still inside the gate (the flock rides on its open descriptor):
        the sweep leaves the lock and says why, rather than deleting a lock
        somebody is inside of."""
        run_id = self.a_run(phase="merge_gate")
        store.release(self.conn, run_id, "failed", "judged dead early",
                      now=T0 + MINUTE)
        path = holophyte.gates.merge_lock_path(self.tgt)
        with holophyte.gates.merge_lock(self.tgt, run_id):
            stamp = path.read_text()
            acted = self.run_sweep(T0 + 2 * MINUTE, "--act")
            self.assertEqual(path.read_text(), stamp)
        self.assertTrue(any("process is alive and holds it; left alone" in line
                            and f"run {run_id}" in line for line in acted), acted)
        self.assertFalse(path.exists())  # the holder's release, not the sweep's

    def test_judging_and_removing_a_stale_lock_is_one_step_against_a_gate(self):
        """The interleaving that removed a live lock: a sweep opened the
        stale inode, paused, and acted after another sweep had cleared it
        and a gate had taken a fresh lock at the same path -- so two gates
        merged at once. Judging and removing now happen under the arbiter a
        gate's create also needs: while it is held, neither a sweep nor a
        gate makes progress; once released, the stale lock goes exactly
        once, the gate holds a fresh one, and a further sweep finds it in
        use rather than displacing it."""
        ended = self.a_run(phase="merge_gate")
        store.release(self.conn, ended, "failed", "died at the gate",
                      now=T0 + MINUTE)
        path = self.lock_for(ended)
        stale = path.read_text()
        live = self.a_run(phase="merge_gate")
        lines, entered = [], threading.Event()
        sweeping = threading.Thread(
            target=lambda: lines.extend(self.run_sweep(T0 + 2 * MINUTE, "--act")))

        def gate():
            with holophyte.gates.merge_lock(self.tgt, live, wait=10, poll=0.01):
                entered.set()
                lines.extend(self.run_sweep(T0 + 2 * MINUTE, "--act"))
        gating = threading.Thread(target=gate)

        with holophyte.gates.merge_lock_arbiter(path):
            sweeping.start()
            gating.start()
            sweeping.join(0.3)
            self.assertTrue(sweeping.is_alive(), "the sweep acted without the arbiter")
            self.assertFalse(entered.is_set(), "the gate entered without the arbiter")
            self.assertEqual(path.read_text(), stale)
        sweeping.join(5)
        gating.join(5)

        self.assertFalse(sweeping.is_alive() or gating.is_alive())
        self.assertIn(f"removed stale merge lock: run {ended} ended", lines)
        self.assertTrue(any(line.startswith(f"merge lock held by run {live}")
                            for line in lines), lines)
        self.assertNotIn("already cleared", " ".join(lines))
        self.assertFalse(path.exists())  # the gate's own release, last
        self.assertEqual(list(path.parent.glob("merge.lock")), [])


class UnavailableStoreTests(SweepTestCase):
    def test_skipped_passes_recover_reset_strikes_and_stop_after_three(self):
        error = sqlite3.OperationalError("locking protocol")
        for outcomes, expected_code, skips in (
                ([error, None], 0, 1),
                ([error, error, None, error, error, None], 0, 4),
                ([error, error, error], 1, 3)):
            with self.subTest(outcomes=outcomes):
                out = io.StringIO()
                waits = []

                def wait(interval):
                    waits.append(interval)
                    if len(waits) == len(outcomes):
                        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

                with patch.object(holophyte.supervisor, "supervise_pass",
                                  side_effect=outcomes) as run_pass, \
                        patch.object(holophyte.supervisor, "factory_revision",
                                     return_value="unchanged"):
                    code = holophyte.supervisor.supervise(
                        self.tgt, interval=7, wait=wait, out=out)
                self.assertEqual(code, expected_code)
                self.assertEqual(run_pass.call_count, len(outcomes))
                lines = [line for line in out.getvalue().splitlines()
                         if "supervisor pass skipped:" in line]
                self.assertEqual(len(lines), skips)
                self.assertIn("store unavailable (locking protocol)", lines[0])
                self.assertIn("next pass in 7s", lines[0])
                self.assertEqual(len(waits), len(outcomes) - bool(expected_code))


class MigrationStartupTests(SweepTestCase):
    def test_owner_stamp_while_waiting_does_not_record_another_migration(self):
        from contextlib import contextmanager

        from holophyte.schema_owner import migrate_store

        self.conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION - 1}")

        @contextmanager
        def lock(*args, **kwargs):
            # Another supervisor finishes before this one acquires the lock.
            self.conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION}")
            with holophyte.gates.merge_lock(*args, **kwargs):
                yield

        with patch('holophyte.schema_owner.merge_lock', lock):
            migrate_store(self.tgt)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM runEvents WHERE kind='migration'").fetchone()[0], 0)

    def test_current_store_reaches_sweep_despite_stale_merge_lock(self):
        run_id = self.a_run(phase="merge_gate")
        store.release(self.conn, run_id, "failed", "died at the gate",
                      now=T0 + MINUTE)
        path = holophyte.gates.merge_lock_path(self.tgt)
        path.write_text(f"{run_id} {T0 / 1000:.3f}\n")

        def first_pass(*args, **kwargs):
            holophyte.supervisor.sweep(
                self.tgt, self.conn, T0 + 2 * MINUTE, act=True)
            self.assertFalse(path.exists())
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        with patch('holophyte.merge_lock.lock_nap',
                   side_effect=AssertionError("startup waited on a stale lock")), \
                patch('holophyte.supervisor.factory_revision', return_value='same'), \
                patch('holophyte.supervisor.supervise_pass', first_pass):
            self.assertEqual(holophyte.supervisor.supervise(
                self.tgt, out=io.StringIO()), 0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM runEvents WHERE kind='migration'").fetchone()[0], 0)

    def test_startup_waits_through_long_merge_before_migrating(self):
        import json

        older = store.SCHEMA_VERSION - 1
        self.conn.execute(f"PRAGMA user_version = {older}")
        clock = [0]
        out = io.StringIO()
        holder = holophyte.gates.merge_lock(self.tgt, None)
        path = holder.__enter__()
        self.addCleanup(holder.__exit__, None, None, None)
        stamp = path.read_text()

        def sleep(_seconds):
            self.assertEqual(path.read_text(), stamp)
            self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0],
                             older)
            self.assertEqual(self.conn.execute(
                "SELECT count(*) FROM runEvents WHERE kind='migration'"
            ).fetchone()[0], 0)
            clock[0] += 181
            if clock[0] > 360:
                holder.__exit__(None, None, None)

        def first_pass(*args, **kwargs):
            self.assertGreater(clock[0], 360)
            self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0],
                             store.SCHEMA_VERSION)
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        with patch('holophyte.gates.monotonic', side_effect=lambda: clock[0]), \
                patch('holophyte.gates.sleep', side_effect=sleep), \
                patch('holophyte.supervisor.factory_revision', return_value='same'), \
                patch('holophyte.supervisor.supervise_pass',
                      side_effect=first_pass) as run:
            self.assertEqual(holophyte.supervisor.supervise(self.tgt, out=out), 0)
        run.assert_called_once()
        self.assertIn("waiting for merge lock before migration", out.getvalue())
        row, = self.conn.execute(
            "SELECT runId, projectId, summary FROM runEvents WHERE kind='migration'")
        self.assertEqual(row[:2], (None, self.project))
        self.assertEqual(json.loads(row[2]),
                         {"from": older, "to": store.SCHEMA_VERSION})
        self.assertFalse(path.exists())

    def test_stop_during_migration_contention_preserves_store_and_holder(self):
        older = store.SCHEMA_VERSION - 1
        self.conn.execute(f"PRAGMA user_version = {older}")
        clock = [0]

        def sleep(_seconds):
            clock[0] += 181
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        with holophyte.gates.merge_lock(self.tgt, None) as path:
            stamp = path.read_text()
            with patch('holophyte.gates.monotonic', side_effect=lambda: clock[0]), \
                    patch('holophyte.gates.sleep', side_effect=sleep), \
                    patch('holophyte.supervisor.factory_revision',
                          return_value='same'), \
                    patch('holophyte.supervisor.supervise_pass') as run:
                self.assertEqual(holophyte.supervisor.supervise(
                    self.tgt, out=io.StringIO()), 0)
            run.assert_not_called()
            self.assertEqual(path.read_text(), stamp)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], older)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM runEvents WHERE kind='migration'").fetchone()[0], 0)

    def test_startup_migrates_under_merge_lock_once(self):
        import json
        from contextlib import contextmanager

        from holophyte.schema_owner import migrate_store

        older = store.SCHEMA_VERSION - 1
        self.conn.execute(f"PRAGMA user_version = {older}")
        held = []
        real_open = store.open

        @contextmanager
        def lock(*args, **kwargs):
            held.append(True)
            try:
                yield
            finally:
                held.pop()

        def opening(*args, **kwargs):
            self.assertTrue(held, "migration must hold the merge lock")
            return real_open(*args, **kwargs)

        with patch('holophyte.schema_owner.merge_lock', lock), \
                patch('holophyte.schema_owner.store.open', opening):
            migrate_store(self.tgt)
            migrate_store(self.tgt)
        row, = self.conn.execute(
            "SELECT runId, projectId, summary FROM runEvents WHERE kind='migration'")
        self.assertEqual(row[:2], (None, self.project))
        self.assertEqual(json.loads(row[2]),
                         {"from": older, "to": store.SCHEMA_VERSION})

    def test_supervise_calls_owner_before_first_pass(self):
        older = store.SCHEMA_VERSION - 1
        self.conn.execute(f"PRAGMA user_version = {older}")

        def first_pass(*args, **kwargs):
            self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0],
                             store.SCHEMA_VERSION)
            raise RuntimeError("stop after startup")

        with patch('holophyte.supervisor.factory_revision', return_value='same'), \
                patch('holophyte.supervisor.supervise_pass', first_pass):
            with self.assertRaisesRegex(RuntimeError, "stop after startup"):
                holophyte.supervisor.supervise(self.tgt, out=io.StringIO())


if __name__ == "__main__":
    unittest.main()
