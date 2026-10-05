"""The stuck-review trips read code findings and criterion restatements apart.

`review_stuck` compares two rounds' code findings; a criterion the reviewer
restates as unmet carries the same key every round, so it is left out of that
measure and judged on its own: the same criterion unmet in three consecutive
ended rounds trips `criterion_stuck`. Restatements are built by the reviewer
reply parser and rounds go in through `store.record_review_round()`, so the
sweep reads what the loop would have written.

Run: python3 -m unittest discover -s tests -p 'test_review_stuck_criteria.py'
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import holophyte.host.supervisor  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
from holophyte.review.reply_parsing import criteria_findings  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_fixture import MINUTE, T0, SweepTestCase  # noqa: E402

CRITERIA = ["the first criterion", "the second criterion",
            "the third criterion"]


def restated(*unmet):
    reply = "\n".join(
        f"CRITERION {n}: {'not met' if n in unmet else 'met'} - reason {n}"
        for n in range(1, len(CRITERIA) + 1))
    return criteria_findings(reply, CRITERIA)


def code(path, line, severity="p1"):
    return {"path": path, "line": line, "severity": severity,
            "message": f"something at {path}:{line}"}


SCHEMA_ROUND = [code("store/schema.py", 19)] + restated(1, 3)
BOARD_ROUND = [code("store/board.py", 131)] + restated(1, 3)


class StuckCriteriaTestCase(SweepTestCase):
    def rounds(self, run_id, *findings_by_round):
        for number, findings in enumerate(findings_by_round, 1):
            at = T0 + number * 2 * MINUTE
            store.record_review_round(
                self.conn, run_id, number, "changes_requested", "reviewer",
                findings=findings, started_at=at, ended_at=at + MINUTE)

    def sweep(self, run_id, at=T0 + 20 * MINUTE):
        self.heartbeat_at(run_id, at)
        return holophyte.host.supervisor.sweep(self.project, self.conn, at)


class CodeFindingOverlapTests(StuckCriteriaTestCase):
    def test_different_code_findings_with_the_same_restatements_share_nothing(self):
        self.assertEqual(store.findings_overlap(SCHEMA_ROUND, BOARD_ROUND), 0.0)
        run_id = self.a_run(phase="addressing")
        self.rounds(run_id, SCHEMA_ROUND, BOARD_ROUND)

        self.assertEqual(self.sweep(run_id).trips, [])

    def test_a_shared_code_finding_counts_without_the_restatements(self):
        earlier = [code("a.py", 1), code("b.py", 2), code("c.py", 3)]
        later = [code("a.py", 1), code("b.py", 2), code("d.py", 4)]
        earlier, later = earlier + restated(1, 2, 3), later + restated(1, 2, 3)
        self.assertEqual(store.findings_overlap(earlier, later), 0.5)
        run_id = self.a_run(phase="addressing")
        self.rounds(run_id, earlier, later)

        trip, = self.sweep(run_id).trips

        self.assertEqual(trip.condition, "review_stuck")
        self.assertIn("rounds 1 and 2 share 0.50", trip.evidence)

    def test_a_lone_repeated_code_finding_reworded_still_trips(self):
        earlier = [dict(code("a.py", 1), message="Missing cancellation guard")]
        later = [dict(code("a.py", 1),
                      message="Cancellation guard is still missing")]
        earlier, later = earlier + restated(2), later + restated(2)
        self.assertEqual(store.findings_overlap(earlier, later), 1.0)
        run_id = self.a_run(phase="addressing")
        self.rounds(run_id, earlier, later)

        trip, = self.sweep(run_id).trips

        self.assertEqual(trip.condition, "review_stuck")

    def test_rounds_of_restatements_alone_report_no_overlap(self):
        self.assertIsNone(store.findings_overlap(restated(1, 3), restated(1, 3)))
        run_id = self.a_run(phase="reviewing")
        self.rounds(run_id, restated(1, 3), restated(1, 3))

        self.assertEqual(self.sweep(run_id).trips, [])


class PersistentCriterionTests(StuckCriteriaTestCase):
    def test_a_criterion_unmet_three_rounds_running_trips_naming_it(self):
        run_id = self.a_run(phase="addressing")
        self.rounds(run_id,
                    [code("a.py", 1)] + restated(1, 2),
                    [code("b.py", 2)] + restated(2, 3),
                    [code("c.py", 3)] + restated(2))

        trip, = self.sweep(run_id).trips

        self.assertEqual((trip.run_id, trip.condition),
                         (run_id, "criterion_stuck"))
        self.assertIn("criterion 2 restated as unmet", trip.evidence)
        self.assertIn("rounds 1, 2 and 3", trip.evidence)

    def test_a_criterion_met_in_the_newest_round_does_not_trip(self):
        run_id = self.a_run(phase="addressing")
        self.rounds(run_id,
                    [code("a.py", 1)] + restated(2),
                    [code("b.py", 2)] + restated(2),
                    [code("c.py", 3)] + restated(1))

        self.assertEqual(self.sweep(run_id).trips, [])

    def test_a_round_meeting_the_criterion_since_the_verdict_acquits(self):
        run_id = self.a_run(phase="addressing")
        self.rounds(run_id,
                    [code("a.py", 1)] + restated(2),
                    [code("b.py", 2)] + restated(2),
                    [code("c.py", 3)] + restated(2))
        trip, = self.sweep(run_id).trips
        store.record_review_round(
            self.conn, run_id, 4, "changes_requested", "reviewer",
            findings=[code("d.py", 4)], started_at=T0 + 21 * MINUTE,
            ended_at=T0 + 22 * MINUTE)

        outcome = holophyte.host.supervisor.act_on_trip(
            self.project, self.conn, trip)

        self.assertFalse(outcome.acted)
        self.assertEqual(store.run_phase(self.conn, run_id), "addressing")


if __name__ == "__main__":
    unittest.main()
