"""`/runs/N` carries the chain of its ticket's runs up to run N."""
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

CLOCK_KEYS = ("started_ms", "elapsed_ms", "working_ms", "agent_ms", "verify_ms")


class ChainDetailTests(ServeTestCase):
    maxDiff = None

    def setUp(self):
        super().setUp()
        self.seed_sent_back_twice()
        self.start()

    def seed_sent_back_twice(self):
        """Seven turns of work, two babysit send-backs, then a merge."""
        now = int(time() * 1000)
        self.starts = [now - 60 * MIN, now - 10 * MIN, now - MIN]
        self.ends = [self.starts[0] + 5 * MIN, self.starts[1] + 5 * MIN,
                     now - MIN + 19 * SEC]
        self.clocks = [(42 * MIN, 4 * MIN), (0, 0), (12 * SEC, 12 * SEC)]
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
                             now=self.ends[n],
                             pr_url="https://example.test/pull/108")
                    store.tickets.transition(conn, ticket,
                                             "blocked_on_operator")
                    store.babysit(conn, ticket, "sent back", now=self.ends[n])
                else:
                    finish_run(conn, run, "merged", now=self.ends[n])
                working, verify = self.clocks[n]
                conn.execute("UPDATE runs SET workingMs = ?, verifyMs = ?,"
                             " workStartedAt = NULL, verifyStartedAt = NULL"
                             " WHERE id = ?", (working, verify, run))
                conn.commit()
        finally:
            conn.close()

    def detail(self, run):
        code, _, body = self.request("GET", f"/runs/{run}")
        self.assertEqual(code, 200)
        return body

    def test_the_merged_run_lists_every_run_of_its_chain_with_its_own_figures(self):
        body = self.detail(self.runs[2])

        agent = [working - verify for working, verify in self.clocks]
        self.assertEqual(
            [{key: run[key] for key in (
                "id", "outcome", "elapsed_ms", "agent_ms", "turn_count")}
             for run in body["chain"]["runs"]],
            [{"id": self.runs[n], "outcome": outcome,
              "elapsed_ms": self.ends[n] - self.starts[n],
              "agent_ms": agent[n], "turn_count": turns}
             for n, (outcome, turns) in enumerate(
                 [("abandoned", 7), ("abandoned", 0), ("merged", 0)])])
        self.assertEqual(
            (body["chain"]["started_ms"], body["chain"]["agent_ms"]),
            (self.starts[0], sum(agent)))
        self.assertEqual(
            (body["run"]["id"], body["run"]["started_ms"], body["run"]["agent_ms"]),
            (self.runs[2], self.starts[2], agent[2]))

    def test_the_first_run_of_a_chain_is_its_own_chain(self):
        body = self.detail(self.runs[0])

        self.assertEqual([run["id"] for run in body["chain"]["runs"]],
                         [self.runs[0]])
        self.assertEqual({key: body["chain"][key] for key in CLOCK_KEYS},
                         {key: body["run"][key] for key in CLOCK_KEYS})


if __name__ == "__main__":
    import unittest
    unittest.main()
