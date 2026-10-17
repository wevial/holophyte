"""`/runs/N` lists each acceptance criterion with the status and note the
newest review over its chain recorded."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from time import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serve_fixture import MIN, ServeTestCase  # noqa: E402 - after the insert

import store  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from tests.phase_fixture import finish_run  # noqa: E402

FIRST = "Given a lease, then it is requeued"
SECOND = "Given a row, then it names its loop"


class RunCriteriaTests(ServeTestCase):
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
            acceptance_criteria=[FIRST, SECOND],
            verification_commands=["echo ok"], time_box_ms=60 * MIN)

    def claim(self, ticket, at):
        store.tickets.transition(self.conn, ticket, "in_flight")
        run = store.claim(self.conn, self.project_id, ticket, now=at)
        store.set_phase(self.conn, run, "working", now=at)
        return run

    def judged(self, run, rnd, at, *criteria):
        store.record_event(
            self.conn, run, "criteria", f"review round {rnd}", level="detail",
            now=at, payload=json.dumps({"round": rnd, "criteria": [
                {"n": n, "status": status, "note": note, "text": text}
                for n, text, status, note in criteria]}))

    def details(self, *runs):
        self.conn.commit()
        self.start()
        bodies = []
        for run in runs:
            code, _, body = self.request("GET", f"/runs/{run}")
            self.assertEqual(code, 200, body)
            bodies.append(body)
        return bodies

    def test_the_newest_round_judges_every_criterion(self):
        run = self.claim(self.ticket("DEMO-20"), self.now - 30 * MIN)
        self.judged(run, 1, self.now - 20 * MIN,
                    (1, FIRST, "met", "tests/test_a.py::test_one"),
                    (2, SECOND, "not_met", "the row names no loop"))
        self.judged(run, 2, self.now - 10 * MIN,
                    (1, FIRST, "met", "tests/test_a.py::test_one"),
                    (2, SECOND, "met", "tests/test_b.py::test_two"))

        (body,) = self.details(run)

        self.assertEqual(body["criteria"], [
            {"n": 1, "id": "C1", "text": FIRST, "status": "met",
             "note": "tests/test_a.py::test_one", "run": run, "round": 2},
            {"n": 2, "id": "C2", "text": SECOND, "status": "met",
             "note": "tests/test_b.py::test_two", "run": run, "round": 2}])
        self.assertNotIn("criteria", [e["kind"] for e in body["events"]])

    def test_a_requeued_run_starts_from_its_chain_and_a_bare_chain_has_none(self):
        ticket = self.ticket("DEMO-21")
        failed = self.claim(ticket, self.now - 30 * MIN)
        self.judged(failed, 1, self.now - 25 * MIN,
                    (1, FIRST, "unwitnessed", "no CRITERION line in the reply"))
        finish_run(self.conn, failed, "failed", "verify failed",
                   now=self.now - 20 * MIN)
        store.requeue(self.conn, ticket, "rerun it")
        live = self.claim(ticket, self.now - 10 * MIN)
        unjudged = self.claim(self.ticket("DEMO-22"), self.now - 5 * MIN)

        requeued, bare = self.details(live, unjudged)

        self.assertEqual(requeued["criteria"], [
            {"n": 1, "id": "C1", "text": FIRST, "status": "unwitnessed",
             "note": "no CRITERION line in the reply", "run": failed,
             "round": 1}])
        self.assertEqual(bare["criteria"], [])

    def test_chain_entries_preserve_ids_and_derive_ids_for_older_events(self):
        ticket = self.ticket("DEMO-23")
        earlier = self.claim(ticket, self.now - 30 * MIN)
        self.judged(earlier, 1, self.now - 25 * MIN,
                    (4, SECOND, "not_met", "Missing witness"))
        finish_run(self.conn, earlier, "failed", "verify failed",
                   now=self.now - 20 * MIN)
        store.requeue(self.conn, ticket, "rerun it")
        newest = self.claim(ticket, self.now - 10 * MIN)
        store.record_event(
            self.conn, newest, "criteria", "review round 2", level="detail",
            now=self.now - 5 * MIN, payload=json.dumps({
                "round": 2, "criteria": [
                    {"n": 4, "id": "C4", "text": SECOND, "status": "met",
                     "note": "Witnessed"}]}))

        old, new = self.details(earlier, newest)

        self.assertEqual(old["criteria"], [
            {"n": 4, "id": "C4", "text": SECOND, "status": "not_met",
             "note": "Missing witness", "run": earlier, "round": 1}])
        self.assertEqual(new["criteria"], [
            {"n": 4, "id": "C4", "text": SECOND, "status": "met",
             "note": "Witnessed", "run": newest, "round": 2}])


if __name__ == "__main__":
    import unittest
    unittest.main()
