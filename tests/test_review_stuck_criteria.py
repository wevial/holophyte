"""The stuck-review trips read code findings and criterion restatements apart.

`review_stuck` compares two rounds' code findings; a criterion the reviewer
restates as unmet carries the same key every round, so it is left out of that
measure and judged on its own: the same criterion unmet in three consecutive
ended rounds trips `criterion_stuck` when its reason in each round shares at
least `review_overlap_threshold` of its words with its reason in the round
before, so a criterion restated for a new gap each round is still converging
and does not trip. Restatements are built by the reviewer
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
from sweep_fixture import MINUTE, T0, StubProvider, SweepTestCase  # noqa: E402

CRITERIA = ["the first criterion", "the second criterion",
            "the third criterion"]


def restated(*unmet):
    reply = "\n".join(
        f"CRITERION {n}: {'not met' if n in unmet else 'met'} - reason {n}"
        for n in range(1, len(CRITERIA) + 1))
    return criteria_findings(reply, CRITERIA)


def restated_with(reason):
    reply = "\n".join(
        f"CRITERION {n}: not met - {reason}" if n == 2
        else f"CRITERION {n}: met - reason {n}"
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

    def test_different_complaints_at_one_code_location_share_nothing(self):
        earlier = [dict(code("worker.py", 20), message="Missing timeout")]
        later = [dict(code("worker.py", 20), message="Retry count is wrong")]
        earlier, later = earlier + restated(1), later + restated(1)
        self.assertEqual(store.findings_overlap(earlier, later), 0.0)
        run_id = self.a_run(phase="addressing")
        self.rounds(run_id, earlier, later)

        self.assertEqual(self.sweep(run_id).trips, [])

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
            findings=[code("d.py", 4)] + restated(1),
            started_at=T0 + 21 * MINUTE,
            ended_at=T0 + 22 * MINUTE)

        outcome = holophyte.host.supervisor.act_on_trip(
            self.project, self.conn, trip)

        self.assertFalse(outcome.acted)
        self.assertEqual(store.run_phase(self.conn, run_id), "addressing")


NEW_GAPS = (
    "no retained test publishes a plain first revision followed by a"
    " live-block revision and checks its scripts",
    "commented bridge text inside an HTML comment still adds the bridge hash",
    "the old page's script-src misses the theme head hash after a reload")
REPEATED_GAP = ("the old page's script-src still misses the theme head's hash"
                " after a reload")


class RepeatedReasonTests(StuckCriteriaTestCase):
    def converging_run(self, *reasons):
        run_id = self.a_run(phase="addressing")
        self.rounds(run_id, *(
            [code(f"gap_{number}.py", number)] + restated_with(reason)
            for number, reason in enumerate(reasons, 1)))
        return run_id

    def test_a_criterion_restated_for_a_new_gap_each_round_does_not_trip(self):
        run_id = self.converging_run(*NEW_GAPS)

        self.assertEqual(self.sweep(run_id).trips, [])

    def test_a_reworded_reason_counts_and_three_repeats_trip(self):
        run_id = self.converging_run(*NEW_GAPS, REPEATED_GAP)
        self.assertEqual(self.sweep(run_id).trips, [])
        at = T0 + 22 * MINUTE
        store.record_review_round(
            self.conn, run_id, 5, "changes_requested", "reviewer",
            findings=[code("gap_5.py", 5)] + restated_with(REPEATED_GAP),
            started_at=at, ended_at=at + MINUTE)

        trip, = self.sweep(run_id, T0 + 30 * MINUTE).trips

        self.assertEqual(trip.condition, "criterion_stuck")
        self.assertIn("criterion 2 restated as unmet in rounds 3, 4 and 5",
                      trip.evidence)

    def test_the_failure_quotes_the_newest_repeated_reason(self):
        run_id = self.converging_run(*NEW_GAPS, REPEATED_GAP, REPEATED_GAP)
        self.heartbeat_at(run_id, T0 + 20 * MINUTE)

        holophyte.host.supervisor.sweep(
            self.project, self.conn, T0 + 20 * MINUTE, act=True,
            provider=StubProvider())

        expected = ("criterion 2 restated as unmet in rounds 3, 4 and 5",
                    f'"not met \u2014 {REPEATED_GAP}"')
        (event,), = self.conn.execute(
            "SELECT group_concat(summary, '|') FROM runEvents"
            " WHERE runId = ? AND kind = 'supervisor_sweep'", (run_id,))
        (reason,), = self.conn.execute(
            "SELECT outcomeReason FROM runs WHERE id = ?", (run_id,))
        for recorded in (event, reason):
            for part in expected:
                self.assertIn(part, recorded)

    def test_the_quote_is_the_restatement_that_repeated(self):
        timeout = "the request timeout is still unbounded"
        run_id = self.a_run(phase="addressing")
        self.rounds(run_id,
                    [code("a.py", 1)] + restated_with(timeout),
                    [code("b.py", 2)] + restated_with(timeout),
                    [code("c.py", 3)]
                    + restated_with("the migration skips the index backfill")
                    + restated_with(timeout))

        trip, = self.sweep(run_id).trips

        self.assertIn(f'"not met \u2014 {timeout}"', trip.evidence)
        self.assertNotIn("migration", trip.evidence)

    def test_a_threshold_of_one_needs_every_word_repeated(self):
        self.configure("[supervisor]\nreview_overlap_threshold = 1.0\n")
        run_id = self.converging_run(*NEW_GAPS, REPEATED_GAP, REPEATED_GAP)

        self.assertEqual(self.sweep(run_id).trips, [])


if __name__ == "__main__":
    unittest.main()
