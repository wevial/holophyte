"""The supervisor's time box counts the run's turns (KO-340).

The loop gives every implementer turn -- the first, and the fix after each
review round -- the ticket's whole budget as its own cap, so the sweep's box
scales with the review rounds the run has recorded, bounded by its review cap.
Runs 160 and 161 were swept at 46 min inside a legitimate fix round because the
sweep read the box once for the whole run.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_supervisor_sweep import MINUTE, T0, SweepTestCase  # noqa: E402

import holophyte.supervisor  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above


class TimeBoxAllowanceTests(unittest.TestCase):
    """The arithmetic, witnessed without a store: 30 min, grace 1.5."""

    def test_the_box_is_counted_once_per_turn_up_to_the_cap(self):
        allowance = holophyte.supervisor.time_box_allowance
        self.assertEqual(allowance(30, 0, 2, 1.5), 45)
        self.assertEqual(allowance(30, 1, 2, 1.5), 90)
        self.assertEqual(allowance(30, 3, 2, 1.5), 135)


class TimeBoxPerTurnSweepTests(SweepTestCase):
    """A run 46 min old with a 30 min box: overdue on one turn, not on two."""

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
        run_id = self.a_run(budget_min=30, phase="addressing")
        store.set_review_round_cap(self.conn, run_id, 2)
        self.a_round(run_id)

        self.assertEqual(self.sweep_at_46(run_id), [])

    def test_the_same_run_with_no_round_trips_on_its_single_box(self):
        run_id = self.a_run(budget_min=30, phase="working")
        store.set_review_round_cap(self.conn, run_id, 2)

        trip, = self.sweep_at_46(run_id)

        self.assertEqual((trip.run_id, trip.condition), (run_id, "time_box"))
        self.assertIn("46.0 min against a 30 min box × 1 turn", trip.evidence)
        self.assertNotIn("2 turns", trip.evidence)

    def test_a_null_cap_is_bounded_by_the_loops_default(self):
        """No `reviewRoundCap` yet: the default cap bounds the multiplier,
        so three rounds are worth the default's turns and nothing raises."""
        run_id = self.a_run(budget_min=30, phase="addressing")
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


if __name__ == "__main__":
    unittest.main()
