"""`/runs/N` says why run N ended or where it parked, and lists the steer
notes sent over its chain with when the agent read each."""
from __future__ import annotations

import sys
from pathlib import Path
from time import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serve_fixture import MIN, SEC, ServeTestCase  # noqa: E402 - after the insert

import store  # noqa: E402 - after the sys.path insert above
import store.steer_notes  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from tests.phase_fixture import finish_run, park_run  # noqa: E402


class RunNotesTests(ServeTestCase):
    maxDiff = None

    def setUp(self):
        super().setUp()
        self.now = int(time() * 1000)
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.tickets.ensure_project(self.conn, "team-1",
                                                       self.target)

    def ticket(self, key):
        return store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{key}",
            linear_identifier=key, title=f"ticket {key}",
            acceptance_criteria=[f"Given {key}, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=60 * MIN)

    def claim(self, ticket, at, queued=True):
        if queued:
            store.tickets.transition(self.conn, ticket, "in_flight")
        run = store.claim(self.conn, self.project_id, ticket, now=at)
        store.set_phase(self.conn, run, "working", now=at)
        return run

    def detail(self, run):
        self.conn.commit()
        self.start()
        code, _, body = self.request("GET", f"/runs/{run}")
        self.assertEqual(code, 200, body)
        return body

    def test_failed_run_has_its_kind_and_cut_reason_parked_run_its_park_kind(self):
        failed = self.claim(self.ticket("DEMO-7"), self.now - 10 * MIN)
        reason = "verify: 2 tests red. " + "x" * 479
        self.assertEqual(len(reason), 500)
        finish_run(self.conn, failed, "failed", reason,
                   failure_kind="verify", now=self.now - 5 * MIN)
        parked = self.claim(self.ticket("DEMO-8"), self.now - 8 * MIN)
        park_run(self.conn, parked, "awaiting_merge_approval", "waiting on CI",
                 pr_url="https://example.test/pull/8", park_kind="ci",
                 now=self.now - 4 * MIN)

        failed_run = self.detail(failed)["run"]
        parked_run = self.detail(parked)["run"]

        self.assertEqual(failed_run["failure_kind"], "verify")
        self.assertEqual(failed_run["outcome_reason"], reason[:400])
        self.assertIsNone(failed_run["park_kind"])
        self.assertEqual(parked_run["park_kind"], "ci")
        self.assertIsNone(parked_run["outcome_reason"])
        self.assertIsNone(parked_run["failure_kind"])

    def test_chain_notes_oldest_first_with_reader_and_earlier_ones_left_out(self):
        ticket = self.ticket("DEMO-9")
        store.steer_notes.steer(self.conn, ticket, "before any run",
                                "maintainer", now=self.now - 30 * MIN)
        abandoned = self.claim(ticket, self.now - 20 * MIN)
        finish_run(self.conn, abandoned, "abandoned", "operator abandoned",
                   now=self.now - 15 * MIN)
        live = self.claim(ticket, self.now - 10 * MIN, queued=False)
        hint = store.steer_notes.steer(self.conn, ticket, "reuse the lease query",
                                       "maintainer", hint=True,
                                       now=self.now - 9 * MIN)
        read_at = self.now - 8 * MIN + 17 * SEC
        store.steer_notes.consume(self.conn, [hint.id], live, now=read_at)
        amendment = store.steer_notes.steer(
            self.conn, ticket, "also cover the empty case", "maintainer",
            now=self.now - 2 * MIN, interrupt=True)

        notes = self.detail(live)["steer_notes"]

        self.assertEqual([note["note"] for note in notes],
                         ["reuse the lease query", "also cover the empty case"])
        first, second = notes
        self.assertEqual(
            {k: first[k] for k in ("id", "kind", "run", "consumed_by",
                                   "consumed_ms", "interrupt", "withdrawn")},
            {"id": hint.id, "kind": "hint", "run": live, "consumed_by": live,
             "consumed_ms": read_at, "interrupt": False, "withdrawn": False})
        self.assertEqual(
            {k: second[k] for k in ("id", "kind", "run", "consumed_by",
                                    "consumed_ms", "interrupt", "withdrawn")},
            {"id": amendment.id, "kind": "amendment", "run": live,
             "consumed_by": None, "consumed_ms": None, "interrupt": True,
             "withdrawn": False})
        self.assertEqual(first["at"], self.now - 9 * MIN)
        self.assertEqual(first["author"], "maintainer")


if __name__ == "__main__":
    import unittest
    unittest.main()
