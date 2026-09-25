"""KO-737: a claim records the ticket revision it ran, and `/tickets/KO-n`
serves the current and the claimed revision side by side.

Run: python3 -m unittest discover -s tests -p 'test_serve_ticket_revisions.py' -v
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from time import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from tests.serve_fixture import MIN, ServeTestCase  # noqa: E402

CLAIMED_BODY = "## Summary\n\nThe body the run was claimed on.\n"
EDITED_BODY = "## Summary\n\nThe body the board holds now.\n"


class TicketRevisionTests(ServeTestCase):
    """`current`, `claimed` and `revisions` beside the ten keys of before."""

    def mirror(self, conn, project, at, **fields):
        fields = {"title": "ticket 7", "body": CLAIMED_BODY, "priority": 3,
                  "labels": ["ui"], "board_column": "ready", **fields}
        return store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-7",
            linear_identifier="KO-7",
            acceptance_criteria=["Given KO-7, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MIN,
            now=self.now - at * MIN, **fields)

    def seed_revisions(self, claim=True):
        """KO-7 mirrored at revision 1, re-prioritized as revision 2, claimed
        when `claim`, then mirrored with an edited body as revision 3."""
        self.now = int(time() * 1000)
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            ticket = self.mirror(conn, project, 9, priority=4)
            self.mirror(conn, project, 8)
            store.tickets.transition(conn, ticket, "in_flight")
            self.run = (store.claim(conn, project, ticket, now=self.now - 7 * MIN)
                        if claim else None)
            self.mirror(conn, project, 6, body=EDITED_BODY)
        finally:
            conn.close()

    def revision(self, number, at, body):
        return {"revision": number, "at": self.now - at * MIN,
                "author": "board", "title": "ticket 7", "body": body,
                "priority": 3, "labels": ["ui"], "column": "ready"}

    def test_the_claimed_revision_is_served_beside_the_edited_current_one(self):
        self.seed_revisions()
        self.start()

        code, _, body = self.request("GET", "/tickets/KO-7")

        self.assertEqual(code, 200)
        self.assertEqual(body["current"], self.revision(3, 6, EDITED_BODY))
        self.assertEqual(body["claimed"], self.revision(2, 8, CLAIMED_BODY))
        self.assertEqual(body["revisions"], [
            {"revision": n, "at": self.now - at * MIN, "author": "board"}
            for n, at in ((3, 6), (2, 8), (1, 9))])
        with sqlite3.connect(self.db) as conn:
            (claimed,) = conn.execute("SELECT revision FROM runs WHERE id = ?",
                                      (self.run,)).fetchone()
        self.assertEqual(claimed, 2)
        # The ten keys of before answer what they answered.
        self.assertEqual({k: body[k] for k in body if k not in (
            "current", "claimed", "revisions")}, {
            "ticket": "KO-7", "ticket_url": None, "title": "ticket 7",
            "status": "in_flight", "body": EDITED_BODY,
            "acceptance_criteria": ["Given KO-7, then it is worked"],
            "verification_commands": ["echo ok"], "time_box_ms": 25 * MIN,
            "run": self.run, "mirrored_ms": self.now - 6 * MIN})

    def test_no_live_run_claims_nothing(self):
        self.seed_revisions(claim=False)
        self.start()

        code, _, body = self.request("GET", "/tickets/KO-7")

        self.assertEqual(code, 200)
        self.assertIsNone(body["run"])
        self.assertIsNone(body["claimed"])
        self.assertEqual(body["current"]["revision"], 3)

    def test_a_previous_builds_claim_is_served_from_its_snapshot(self):
        self.seed_revisions(claim=False)
        snapshot = json.dumps({
            "title": "ticket 7 as claimed",
            "acceptanceCriteria": ["Given the old KO-7, then it is worked"],
            "verificationCommands": ["echo claimed"], "evidenceStates": []})
        with sqlite3.connect(self.db) as conn:
            # The previous build's frozen run INSERT, which names no revision.
            run = conn.execute(
                "INSERT INTO runs"
                " (ticketId, projectId, attempt, phase, startedAt, lastHeartbeat,"
                "  timeBoxMs, ticketSnapshot, host, workerPid, workingMs, verifyMs)"
                " VALUES (1, 1, 1, 'claimed', ?, ?, ?, ?, ?, ?, 0, 0)",
                (self.now, self.now, 25 * MIN, snapshot, "host", 1)).lastrowid
            conn.execute("UPDATE tickets SET activeRunId = ? WHERE id = 1",
                         (run,))
        self.start()

        code, _, body = self.request("GET", "/tickets/KO-7")

        self.assertEqual(code, 200)
        self.assertEqual(body["run"], run)
        self.assertEqual(body["claimed"], {
            "revision": None, "title": "ticket 7 as claimed",
            "acceptance_criteria": ["Given the old KO-7, then it is worked"],
            "verification_commands": ["echo claimed"]})


if __name__ == "__main__":
    unittest.main()
