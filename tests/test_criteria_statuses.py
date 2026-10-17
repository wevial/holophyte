"""Each criterion's judged status, the findings built from it, and the
`criteria` event a review round records.

Run: python3 -m unittest discover -s tests -p 'test_criteria_statuses.py' -v
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import holophyte.loop.runs
import store
import store.tickets
from holophyte.review.reply_parsing import (
    MISSING_WITNESS_NOTE,
    UNWITNESSED_NOTE,
    criteria_findings,
    criteria_statuses,
)

CRITERIA = ["Given a lease, then it is requeued",
            "Given a row, then it names its loop",
            "Given a day, then it is logged"]
REPLY = ("Looks right.\n"
         "CRITERION 1: met — tests/test_a.py::test_one\n"
         "CRITERION 2: not met — the row names loop\n"
         "VERDICT: APPROVE")


class WorktreeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.name", "Test reviewer"),
                     ("config", "user.email", "reviewer@example.test"),
                     ("commit", "-q", "--allow-empty", "-m", "base")):
            subprocess.run(["git", *args], cwd=self.root, check=True)

    def add_witness(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_a.py").write_text(
            "import unittest\n\n\nclass A(unittest.TestCase):\n"
            "    def test_one(self):\n        pass\n")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "witness"], cwd=self.root,
                       check=True)


class CriteriaStatusesTests(WorktreeCase):
    def test_each_criterion_reads_met_not_met_or_unwitnessed(self):
        self.add_witness()
        self.assertEqual(criteria_statuses(REPLY, CRITERIA, self.root), [
            {"n": 1, "status": "met", "note": "tests/test_a.py::test_one"},
            {"n": 2, "status": "not_met", "note": "the row names loop"},
            {"n": 3, "status": "unwitnessed", "note": UNWITNESSED_NOTE}])

    def test_met_line_naming_a_missing_test_reads_unwitnessed(self):
        first = criteria_statuses(REPLY, CRITERIA, self.root)[0]
        self.assertEqual(first["status"], "unwitnessed")
        self.assertTrue(first["note"].startswith(MISSING_WITNESS_NOTE),
                        first["note"])


class CriteriaFindingsAgreeTests(WorktreeCase):
    def assert_findings_are_the_unmet_statuses(self):
        statuses = criteria_statuses(REPLY, CRITERIA, self.root)
        findings = criteria_findings(REPLY, CRITERIA, self.root)
        self.assertEqual([f["line"] for f in findings],
                         [s["n"] for s in statuses if s["status"] != "met"])
        return findings

    def test_findings_cover_only_criteria_not_met_or_unwitnessed(self):
        self.add_witness()
        findings = self.assert_findings_are_the_unmet_statuses()
        self.assertEqual([f["message"] for f in findings], [
            f"CRITERION C2: not met — the row names loop\n{CRITERIA[1]}",
            f"CRITERION C3: unwitnessed — {UNWITNESSED_NOTE}\n"
            f"{CRITERIA[2]}"])

    def test_missing_witness_finding_carries_the_missing_note(self):
        findings = self.assert_findings_are_the_unmet_statuses()
        self.assertTrue(findings[0]["message"].startswith(
            f"CRITERION C1: unwitnessed — {MISSING_WITNESS_NOTE}"),
            findings[0]["message"])


class CriteriaEventTests(WorktreeCase):
    def setUp(self):
        super().setUp()
        self.add_witness()
        self.conn = store.open(str(self.root / "store.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = store.tickets.ensure_project(self.conn, "team-1", self.root)
        ticket = store.tickets.mirror_ticket(
            self.conn, project, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="ticket 1",
            acceptance_criteria=CRITERIA, verification_commands=["echo ok"],
            time_box_ms=60_000)
        store.tickets.transition(self.conn, ticket, "in_flight")
        self.run_id = store.claim(self.conn, project, ticket)

    def record(self, reply):
        holophyte.loop.runs.record_round(
            None, self.conn, self.run_id, 1, "review", reply, "echo ok", True,
            "ok", criteria=CRITERIA, root=self.root, route="reviewer")

    def criteria_events(self):
        return self.conn.execute(
            "SELECT level, payload FROM runEvents"
            " WHERE runId = ? AND kind = 'criteria'", (self.run_id,)).fetchall()

    def test_review_round_records_every_criterion_with_text_and_status(self):
        self.record(REPLY)
        ((level, payload),) = self.criteria_events()
        self.assertEqual(level, "detail")
        event = json.loads(payload)
        self.assertEqual(event["round"], 1)
        self.assertEqual(
            [(c["n"], c["text"], c["status"]) for c in event["criteria"]],
            [(1, CRITERIA[0], "met"), (2, CRITERIA[1], "not_met"),
             (3, CRITERIA[2], "unwitnessed")])

    def test_error_round_records_no_criteria_event(self):
        self.record("The reviewer crashed before a verdict.")
        ((verdict,),) = self.conn.execute(
            "SELECT verdict FROM reviewRounds WHERE runId = ?",
            (self.run_id,)).fetchall()
        self.assertEqual(verdict, "error")
        self.assertEqual(self.criteria_events(), [])


if __name__ == "__main__":
    unittest.main()
