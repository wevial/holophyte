"""A review round keeps the reviewer's schema-checked JSON reply: the run
response's criteria and the description-only findings come from it, a round
with no stored reply reads as its text did, and an older store gains the
column without losing a round's findings."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from time import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serve_fixture import MIN, ServeTestCase  # noqa: E402 - after the insert

import store  # noqa: E402 - after the sys.path insert above
import store.read  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from holophyte import redact  # noqa: E402
from holophyte.babysit.description_review import (  # noqa: E402
    description_findings,
    round_description_findings,
)
from holophyte.loop.runs import record_round  # noqa: E402
from holophyte.review.json_reply import read_reply  # noqa: E402

FIRST = "Given a lease, then it is requeued"
SECOND = "Given a row, then it names its loop"
DECOY = "CRITERION C9: met — not a criterion line"


def json_reply(*criteria, findings=()):
    return json.dumps({
        "verdict": "REQUEST_CHANGES",
        "criteria": [{"id": cid, "status": status, "reason": reason,
                      "witness": witness}
                     for cid, status, reason, witness in criteria],
        "findings": list(findings), "follow_ups": [], "scope": [],
        "refuted": []})


class RoundReplyTests(ServeTestCase):
    maxDiff = None

    def setUp(self):
        super().setUp()
        self.now = int(time() * 1000)
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        project = store.tickets.ensure_project(self.conn, "team-1", self.target)
        ticket = store.tickets.mirror_ticket(
            self.conn, project, linear_issue_id="issue-1",
            linear_identifier="DEMO-1", title="ticket DEMO-1",
            acceptance_criteria=[FIRST, SECOND],
            verification_commands=["echo ok"], time_box_ms=60 * MIN)
        store.tickets.transition(self.conn, ticket, "in_flight")
        self.run = store.claim(self.conn, project, ticket, now=self.now - MIN)
        store.set_review_round_cap(self.conn, self.run, 1)
        store.record_review_round(self.conn, self.run, 1, "changes_requested",
                                  "reviewer")

    def final_round(self, reply, root=None):
        record_round(None, self.conn, self.run, 2, "review", reply, None, True,
                     "", criteria=[FIRST, SECOND], route="reviewer", root=root)
        return self.served()

    def served(self):
        self.conn.commit()
        self.start()
        code, _, body = self.request("GET", f"/runs/{self.run}")
        self.assertEqual(code, 200, body)
        return body

    def test_a_json_round_serves_its_reply_criteria_and_its_description_finding(self):
        decoy = {"path": None, "line": None, "severity": "p1",
                 "category": "docs", "target": "description", "message": DECOY}
        reply, reply_format = read_reply(json_reply(
            ("C1", "met", "requeued", "tests/test_a.py::T::test_one"),
            ("C2", "met", "names it", "tests/test_b.py::T::test_two"),
            findings=[decoy]))
        self.assertEqual(reply_format, "json")

        body = self.final_round(reply)

        self.assertEqual(body["rounds"][1]["criteria"], [
            {"id": "C1", "n": 1, "status": "met",
             "note": "requeued; witness: tests/test_a.py::T::test_one"},
            {"id": "C2", "n": 2, "status": "met",
             "note": "names it; witness: tests/test_b.py::T::test_two"}])
        untagged = reply.replace('path: "PR description" ', "")
        self.assertEqual(
            description_findings(untagged, "REQUEST_CHANGES", []), [])
        described = round_description_findings(
            self.conn, self.run, (untagged, "REQUEST_CHANGES", [], 2))
        self.assertEqual(len(described), 1)
        self.assertIn(DECOY, described[0])

    def test_a_json_final_check_keeps_the_factory_downgrade_of_a_missing_witness(self):
        reply, _ = read_reply(json_reply(
            ("C1", "met", "requeued", "tests/test_a.py::T::test_one"),
            ("C2", "met", "names it", "tests/test_b.py::T::test_two")))

        body = self.final_round(reply, root=self.root)

        self.assertEqual([(c["id"], c["status"])
                          for c in body["rounds"][1]["criteria"]],
                         [("C1", "unwitnessed"), ("C2", "unwitnessed")])
        self.assertEqual(body["rounds"][1]["criteria"], [
            {key: c[key] for key in ("id", "n", "status", "note")}
            for c in body["criteria"]])

    def test_a_stored_reply_holds_no_registered_secret(self):
        reply, _ = read_reply(json_reply(
            ("C1", "met", "token SECRET-TOKEN-1 works",
             "tests/test_a.py::T::test_one"),
            ("C2", "met", "names it", "tests/test_b.py::T::test_two")))

        with redact.values_held(["SECRET-TOKEN-1"]):
            body = self.final_round(reply)

        self.assertNotIn("SECRET-TOKEN-1",
                         store.read.rounds_of(self.conn, self.run)[1].reply)
        self.assertNotIn("SECRET-TOKEN-1",
                         json.dumps(body["rounds"][1]["criteria"]))

    def test_a_round_without_a_reply_reads_its_criteria_and_findings_from_text(self):
        reply = ("- path: \"PR description\" [p1] the summary names the wrong"
                 " module\n\n"
                 "CRITERION C1: met — requeued; witness:"
                 " tests/test_a.py::T::test_one\n"
                 "VERDICT: REQUEST_CHANGES")

        body = self.final_round(reply)

        self.assertIsNone(store.read.rounds_of(self.conn, self.run)[1].reply)
        self.assertEqual(body["rounds"][1]["criteria"], [
            {"id": "C1", "n": 1, "status": "met", "note": ""},
            {"id": "C2", "n": 2, "status": "unwitnessed",
             "note": "no CRITERION line in the reply"}])
        self.assertEqual(round_description_findings(
            self.conn, self.run, (reply, "REQUEST_CHANGES", [], 2)),
            ["- path: \"PR description\" [p1] the summary names the wrong"
             " module"])

    def test_a_round_without_a_reply_keeps_a_criterion_the_newest_judgement_lacks(self):
        store.record_event(
            self.conn, self.run, "criteria", "review round 1", level="detail",
            payload=json.dumps({"round": 1, "criteria": [
                {"n": 1, "status": "met", "note": "ok", "text": FIRST}]}))
        store.record_review_round(
            self.conn, self.run, 2, "changes_requested", "adjudicator",
            findings=[{"path": store.CRITERIA_PATH, "line": 2,
                       "severity": "p1",
                       "message": "CRITERION C2: not met \u2014 added late"
                                  f"\n{SECOND}"}])

        body = self.served()

        self.assertEqual(body["rounds"][1]["criteria"], [
            {"id": "C1", "n": 1, "status": "met", "note": ""},
            {"id": "C2", "n": 2, "status": "not_met", "note": "added late"}])


class ReplyColumnMigrationTests(unittest.TestCase):
    def test_a_store_at_the_previous_version_gains_a_null_reply(self):
        findings = [{"path": "app.py", "line": 3, "severity": "p1",
                     "message": "app.py:3 drops the lease"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            conn = store.open(path)
            project = store.tickets.ensure_project(conn, "team", "/repos/h")
            ticket = store.tickets.mirror_ticket(
                conn, project, linear_issue_id="issue-1",
                linear_identifier="KO-1", title="candidate")
            run = store.claim(conn, project, ticket, now=1)
            store.record_review_round(conn, run, 1, "changes_requested",
                                      "reviewer", findings=findings)
            conn.close()
            raw = sqlite3.connect(path)
            raw.executescript(
                "ALTER TABLE reviewRounds DROP COLUMN reply;\n"
                f"PRAGMA user_version = {store.schema.SCHEMA_VERSION - 1};\n")
            raw.close()
            readonly = store.read.open_readonly(path)
            (unmigrated,) = store.read.rounds_of(readonly, run)
            readonly.close()
            self.assertEqual(json.loads(unmigrated.findings), findings)
            self.assertIsNone(unmigrated.reply)

            conn = store.open(path)
            self.addCleanup(conn.close)

            self.assertIn("reply", [row[1] for row in conn.execute(
                "PRAGMA table_info(reviewRounds)")])
            (recorded,) = store.read.rounds_of(conn, run)
            self.assertEqual(json.loads(recorded.findings), findings)
            self.assertIsNone(recorded.reply)


if __name__ == "__main__":
    unittest.main()
