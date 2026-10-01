"""`/shipped` rows report a ticket's chain of runs, not its closing run alone.

Run: python3 -m unittest discover -s tests -p 'test_serve_chains.py' -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from time import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serve_fixture import MIN, SEC, ServeTestCase  # noqa: E402 - after the insert

import store  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from tests.phase_fixture import finish_run, park_run  # noqa: E402


class ShippedChainTests(ServeTestCase):
    maxDiff = None

    def set_clocks(self, conn, run, working, verify):
        conn.execute("UPDATE runs SET workingMs = ?, verifyMs = ?,"
                     " workStartedAt = NULL, verifyStartedAt = NULL"
                     " WHERE id = ?", (working, verify, run))
        conn.commit()

    def seed_sent_back_twice(self):
        """Work, two babysit send-backs, then a merge with verify only."""
        now = int(time() * 1000)
        self.starts = [now - 60 * MIN, now - 10 * MIN, now - MIN]
        self.end = now - MIN + 19 * SEC
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            ticket = store.tickets.mirror_ticket(
                conn, project, linear_issue_id="issue-108",
                linear_identifier="KO-108", title="ticket 108",
                acceptance_criteria=["Given 108, then it is worked"],
                verification_commands=["echo ok"], time_box_ms=60 * MIN)
            self.runs = []
            clocks = [(42 * MIN, 4 * MIN), (0, 0), (12 * SEC, 12 * SEC)]
            for n, started in enumerate(self.starts):
                store.tickets.transition(conn, ticket, "in_flight")
                run = store.claim(conn, project, ticket, now=started)
                self.runs.append(run)
                if n == 0:
                    for _ in range(7):
                        store.record_event(
                            conn, run, "agent_turn", "ended", level="detail",
                            payload=json.dumps(dict(role="implement",
                                                    route="primary",
                                                    seconds=60)))
                if n < 2:
                    park_run(conn, run, "awaiting_merge_approval",
                             now=started + 5 * MIN,
                             pr_url="https://example.test/pull/108")
                    store.tickets.transition(conn, ticket,
                                             "blocked_on_operator")
                    store.babysit(conn, ticket, "sent back",
                                  now=started + 5 * MIN)
                else:
                    finish_run(conn, run, "merged", now=self.end)
                self.set_clocks(conn, run, *clocks[n])
            self.clocks = clocks
        finally:
            conn.close()

    def test_a_sent_back_ticket_reports_its_whole_chain(self):
        self.seed_sent_back_twice()
        self.start()

        code, _, body = self.request("GET", "/shipped")

        self.assertEqual(code, 200)
        [row] = [r for r in body["rows"] if r["ticket"] == "KO-108"]
        working = sum(w for w, _ in self.clocks)
        verify = sum(v for _, v in self.clocks)
        self.assertEqual(
            {key: row.get(key) for key in (
                "id", "started_ms", "wall_min", "working_ms", "agent_ms",
                "verify_ms", "turn_count", "run_count")},
            {"id": self.runs[2], "started_ms": self.starts[0],
             "wall_min": (self.end - self.starts[0]) / 60000,
             "working_ms": working, "agent_ms": working - verify,
             "verify_ms": verify, "turn_count": 7, "run_count": 3})
