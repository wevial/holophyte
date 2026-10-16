"""`/findings?since=MS`: review rounds, round ledger rows, advisories and
follow-ups across runs, served over the socket by a real daemon.

Run: python3 -m unittest discover -s tests -p 'test_serve_findings*' -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from time import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serve_fixture import MIN, ServeTestCase  # noqa: E402 - after the insert

import store  # noqa: E402 - after the sys.path insert above
import store.follow_ups  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from tests.phase_fixture import finish_run  # noqa: E402

DAY = 24 * 60 * MIN
FINDINGS = [{"severity": "p1", "message": "the limit is not echoed",
             "path": "holophyte/serve/serve_findings.py", "line": 40},
            {"severity": "nit", "message": "rename the helper",
             "path": "holophyte/serve/serve_findings.py", "line": 12}]
ROUND_TEXT = ("Round 1: changes_requested · reviewer reviewer-model\n"
              "Implementer response:\n"
              "ADDRESS: the limit is now echoed\n"
              "DECLINE: the helper's name matches its siblings")


class FindingsCase(ServeTestCase):
    """A seeded store; each test writes its runs with `ended_run`."""

    def setUp(self):
        super().setUp()
        self.now = int(time() * 1000)
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.tickets.ensure_project(self.conn, "team-1",
                                                       self.target)

    def ended_run(self, n, rounds, started, write=None):
        """One run on ticket KO-n with `rounds` as `(verdict, findings,
        ended_at)`, `write(run)` called before it merges."""
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{n}",
            linear_identifier=f"KO-{n}", title=f"ticket {n}",
            acceptance_criteria=[f"Given ticket {n}, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MIN)
        store.tickets.transition(self.conn, ticket, "in_flight")
        run = store.claim(self.conn, self.project_id, ticket, now=started)
        for number, (verdict, findings, ended_at) in enumerate(rounds, 1):
            store.record_review_round(
                self.conn, run, number, verdict, "reviewer-model",
                findings=findings, started_at=ended_at - MIN,
                ended_at=ended_at)
        if write is not None:
            write(run)
        last = rounds[-1][2] if rounds else started + MIN
        finish_run(self.conn, run, "merged", now=last + MIN)
        return run


class FindingsWindowTests(FindingsCase):

    def test_the_window_lists_runs_with_a_round_since_and_their_rounds_as_runs_n(self):
        a = self.ended_run(1, [("changes_requested", FINDINGS,
                                self.now - DAY),
                               ("pass", [], self.now - DAY + 10 * MIN)],
                           started=self.now - DAY - 30 * MIN)
        self.ended_run(2, [("pass", [], self.now - 10 * DAY)],
                       started=self.now - 10 * DAY - 30 * MIN)
        self.start()

        code, _, body = self.request(
            "GET", f"/findings?since={self.now - 7 * DAY}")

        self.assertEqual(code, 200)
        self.assertEqual([r["run"] for r in body["runs"]], [a])
        [run] = body["runs"]
        self.assertEqual(run["ticket"], "KO-1")
        self.assertEqual([r["verdict"] for r in run["rounds"]],
                         ["changes_requested", "pass"])
        self.assertEqual([f["message"] for f in run["rounds"][0]["findings"]],
                         [f["message"] for f in FINDINGS])
        code, _, detail = self.request("GET", f"/runs/{a}")
        self.assertEqual(code, 200)
        self.assertEqual(run["rounds"], detail["rounds"])

    def test_a_runs_ledger_holds_its_round_rows_whole_and_its_advisories(self):
        def write(run):
            at = self.now - DAY
            store.record_ledger(self.conn, run, "round", ROUND_TEXT, now=at)
            store.record_ledger(self.conn, run, "intervention",
                                "answered: keep the flag name",
                                source="operator", now=at + MIN)
            store.record_ledger(self.conn, run, "merge", "MERGED to main",
                                now=at + 2 * MIN)
            store.record_event(self.conn, run, "bot_finding",
                               "https://example.test/pull/1#r1: nit",
                               now=at)

        self.ended_run(1, [("changes_requested", FINDINGS, self.now - DAY)],
                       started=self.now - DAY - 30 * MIN, write=write)
        self.start()

        code, _, body = self.request(
            "GET", f"/findings?since={self.now - 7 * DAY}")

        self.assertEqual(code, 200)
        [run] = body["runs"]
        self.assertEqual(run["ledger"],
                         [{"at": self.now - DAY, "kind": "round",
                           "text": ROUND_TEXT, "source": "loop"}])
        self.assertEqual(run["advisories"],
                         [{"tone": "advisory",
                           "message": "https://example.test/pull/1#r1: nit"}])

    def test_follow_ups_since_or_pending_come_newest_first_with_their_state(self):
        rows = {}

        def write(run):
            def follow_up(name, kind, created):
                rows[name] = store.follow_ups.record_follow_up(
                    self.conn, run, "f" * 40, kind, True, f"{name} text",
                    fingerprint=name, now=created)

            yesterday = self.now - DAY
            follow_up("pending", "feature", yesterday)
            follow_up("filed", "feature", yesterday + MIN)
            follow_up("ledger", "guardrail", yesterday + 2 * MIN)
            follow_up("superseded", "feature", yesterday + 3 * MIN)
            follow_up("old settled", "feature", self.now - 30 * DAY)
            follow_up("old pending", "guardrail", self.now - 31 * DAY)
            settled = yesterday + 10 * MIN
            store.follow_ups.settle_filed(self.conn, rows["filed"], "KO-90",
                                          now=settled)
            store.follow_ups.settle_ledger(self.conn, rows["ledger"],
                                           now=settled)
            store.follow_ups.settle_superseded(self.conn, rows["superseded"],
                                               "a" * 40, now=settled)
            store.follow_ups.settle_filed(self.conn, rows["old settled"],
                                          "KO-91", now=settled)

        self.ended_run(1, [("pass", [], self.now - DAY)],
                       started=self.now - DAY - 30 * MIN, write=write)
        self.start()

        code, _, body = self.request(
            "GET", f"/findings?since={self.now - 7 * DAY}")

        self.assertEqual(code, 200)
        self.assertEqual(
            [(f["id"], f["state"], f["filed_as"]) for f in body["follow_ups"]],
            [(rows["superseded"], "superseded", None),
             (rows["ledger"], "ledger", None),
             (rows["filed"], "filed", "KO-90"),
             (rows["pending"], "pending", None),
             (rows["old pending"], "pending", None)])
        pending = body["follow_ups"][3]
        self.assertEqual((pending["ticket"], pending["text"],
                          pending["created_ms"], pending["settled_ms"]),
                         ("KO-1", "pending text", self.now - DAY, None))


class FindingsWindowParameterTests(FindingsCase):

    def test_a_missing_or_bad_parameter_is_400_naming_it(self):
        self.start()
        for query, name in (("", "since"), ("since=abc", "since"),
                            (f"since={self.now}&limit=0", "limit")):
            with self.subTest(query=query):
                code, _, body = self.request("GET", f"/findings?{query}")
                self.assertEqual(code, 400)
                self.assertIn(name, body["error"])

    def test_no_store_is_503(self):
        self.conn.close()
        self.db.unlink()
        self.start()

        code, _, body = self.request("GET", f"/findings?since={self.now}")

        self.assertEqual(code, 503)
        self.assertEqual(body["error"], "no store")

    def test_limit_keeps_the_runs_with_the_newest_rounds(self):
        ended = {1: self.now - 3 * DAY, 2: self.now - DAY,
                 3: self.now - 2 * DAY}
        runs = {n: self.ended_run(n, [("pass", [], at)],
                                  started=at - 30 * MIN)
                for n, at in ended.items()}
        self.start()

        code, _, body = self.request(
            "GET", f"/findings?since={self.now - 7 * DAY}&limit=2")

        self.assertEqual(code, 200)
        self.assertEqual(body["limit"], 2)
        self.assertEqual([r["run"] for r in body["runs"]], [runs[2], runs[3]])


if __name__ == "__main__":
    unittest.main()
