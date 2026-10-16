"""The findings built from each criterion's judged status, and the `criteria`
event a review round records.

Run: python3 -m unittest discover -s tests -p 'test_criteria_statuses.py' -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_fixture import SweepTestCase  # noqa: E402

import holophyte.loop.runs  # noqa: E402
from holophyte.review.reply_parsing import (  # noqa: E402
    UNWITNESSED_NOTE,
    criteria_findings,
)

CRITERIA = ["Given a lease, then it is requeued",
            "Given a row, then it names its loop",
            "Given a day, then it is logged"]
REPLY = ("Looks right.\n"
         "CRITERION 1: met — tests/test_a.py::test_one\n"
         "CRITERION 2: not met — the row names loop\n"
         "VERDICT: APPROVE")


class CriteriaStatusTests(SweepTestCase):
    def setUp(self):
        super().setUp()
        (self.target / "tests").mkdir()
        (self.target / "tests" / "test_a.py").write_text(
            "import unittest\n\n\nclass A(unittest.TestCase):\n"
            "    def test_one(self):\n        pass\n")
        self.run_id = self.a_run(phase="claimed")

    def record(self, reply):
        holophyte.loop.runs.record_round(
            None, self.conn, self.run_id, 1, "review", reply, "echo ok", True,
            "ok", criteria=CRITERIA, root=self.target, route="reviewer")

    def criteria_events(self):
        return self.conn.execute(
            "SELECT level, payload FROM runEvents"
            " WHERE runId = ? AND kind = 'criteria'", (self.run_id,)).fetchall()

    def test_findings_cover_only_criteria_not_met_or_unwitnessed(self):
        findings = criteria_findings(REPLY, CRITERIA, self.target)
        self.assertEqual([f["message"] for f in findings], [
            f"CRITERION 2: not met — the row names loop\n{CRITERIA[1]}",
            f"CRITERION 3: unwitnessed — {UNWITNESSED_NOTE}\n"
            f"{CRITERIA[2]}"])

    def test_review_round_records_every_criterion_with_text_and_status(self):
        self.record(REPLY)
        ((level, payload),) = self.criteria_events()
        self.assertEqual(level, "detail")
        self.assertEqual(json.loads(payload), {"round": 1, "criteria": [
            {"n": 1, "status": "met", "note": "tests/test_a.py::test_one",
             "text": CRITERIA[0]},
            {"n": 2, "status": "not_met", "note": "the row names loop",
             "text": CRITERIA[1]},
            {"n": 3, "status": "unwitnessed", "note": UNWITNESSED_NOTE,
             "text": CRITERIA[2]}]})

    def test_error_round_records_no_criteria_event(self):
        self.record("The reviewer crashed before a verdict.")
        ((verdict,),) = self.conn.execute(
            "SELECT verdict FROM reviewRounds WHERE runId = ?",
            (self.run_id,)).fetchall()
        self.assertEqual(verdict, "error")
        self.assertEqual(self.criteria_events(), [])


if __name__ == "__main__":
    unittest.main()
