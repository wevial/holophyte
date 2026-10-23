"""`/runs/N` lists each acceptance criterion with the status and note the
newest review over its chain recorded, each round's own judgement, the
final check's verdicts and the run's first witness pre-check."""
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

    def reviewed(self, key):
        run = self.claim(self.ticket(key), self.now - 30 * MIN)
        store.set_review_round_cap(self.conn, run, 3)
        self.judged(run, 1, self.now - 25 * MIN,
                    (1, FIRST, "met", "tests/test_a.py::test_one"),
                    (2, SECOND, "met", "tests/test_b.py::test_two"))
        self.judged(run, 1, self.now - 24 * MIN,
                    (1, FIRST, "not_met", "export is absent"),
                    (2, SECOND, "met", "tests/test_b.py::test_two"))
        for rnd in (1, 2, 3):
            store.record_review_round(self.conn, run, rnd, "changes_requested",
                                      "reviewer")
        return run

    def test_each_review_round_serves_its_own_newest_judgement(self):
        run = self.reviewed("DEMO-24")

        (body,) = self.details(run)

        self.assertEqual(body["rounds"][0]["criteria"], [
            {"id": "C1", "n": 1, "status": "not_met",
             "note": "export is absent"},
            {"id": "C2", "n": 2, "status": "met",
             "note": "tests/test_b.py::test_two"}])
        self.assertEqual(body["rounds"][1]["criteria"], [])

    def test_the_final_check_marks_its_criterion_findings_and_the_rest_met(self):
        run = self.reviewed("DEMO-25")
        store.record_review_round(
            self.conn, run, 4, "changes_requested", "reviewer", findings=[
                {"path": store.CRITERIA_PATH, "line": 2, "severity": "p1",
                 "message": "CRITERION C2: unwitnessed \u2014 no CRITERION "
                            f"line in the reply\n{SECOND}"}])

        (body,) = self.details(run)

        self.assertEqual(body["run"]["max_rounds"], 3)
        self.assertEqual(body["rounds"][3]["criteria"], [
            {"id": "C1", "n": 1, "status": "met", "note": ""},
            {"id": "C2", "n": 2, "status": "unwitnessed",
             "note": "no CRITERION line in the reply"}])

    def test_a_final_check_that_errored_judges_nothing(self):
        run = self.reviewed("DEMO-26")
        store.record_review_round(
            self.conn, run, 4, "error", "reviewer", findings=[
                {"path": "(unparsed)", "severity": "p1",
                 "message": "the reviewer answered no verdict"}])

        (body,) = self.details(run)

        self.assertEqual(body["rounds"][3]["criteria"], [])

    def prechecked(self, run, sha, *criteria):
        store.record_event(
            self.conn, run, "witness_precheck_results", "witness pre-check",
            level="detail", payload=json.dumps({
                "sha": sha, "base": "b" * 40, "criteria": [
                    {"criterion": n, "id": f"C{n}", "files": files,
                     "named": "ok" if files else "no_file", "checks": [],
                     "gaps": gaps} for n, files, gaps in criteria]}))

    def test_the_first_witness_precheck_is_served_and_none_is_null(self):
        run = self.claim(self.ticket("DEMO-27"), self.now - 30 * MIN)
        self.prechecked(run, "1" * 40, (1, ["tests/test_a.py"], []), (2, [], []))
        self.prechecked(run, "2" * 40,
                        (1, ["tests/test_a.py"],
                         ["criterion C1 names `tests/test_a.py`; it fails"]),
                        (2, ["tests/test_b.py"], []))
        bare = self.claim(self.ticket("DEMO-28"), self.now - 5 * MIN)

        checked, unchecked = self.details(run, bare)

        self.assertEqual(checked["precheck"], {"sha": "1" * 40, "criteria": [
            {"id": "C1", "n": 1, "witnessed": True, "note": ""},
            {"id": "C2", "n": 2, "witnessed": False,
             "note": "names no test file"}]})
        self.assertIsNone(unchecked["precheck"])


if __name__ == "__main__":
    import unittest
    unittest.main()
