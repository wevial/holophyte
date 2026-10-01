"""`/shipped` and `/status` rows report a ticket's chain of runs, not one run alone.

Run: python3 -m unittest discover -s tests -p 'test_serve_chains.py' -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from time import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serve_fixture import MIN, SEC, ServeTestCase  # noqa: E402 - after the insert

import holophyte.serve.server  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from holophyte.config.project import Project  # noqa: E402
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

    def test_a_continued_run_has_no_finished_row_of_its_own(self):
        self.seed_sent_back_twice()
        self.start()

        code, _, body = self.request("GET", "/shipped?outcome=all")

        self.assertEqual(code, 200)
        self.assertEqual(
            [r["id"] for r in body["rows"] if r["ticket"] == "KO-108"],
            [self.runs[2]])
        self.assertFalse({r["id"] for r in body["rows"]} & set(self.runs[:2]))

    def open_ticket(self, conn, n):
        project = store.tickets.ensure_project(conn, "team-1", self.target)
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id=f"issue-{n}",
            linear_identifier=f"KO-{n}", title=f"ticket {n}",
            acceptance_criteria=[f"Given {n}, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=30 * MIN)
        return project, ticket

    def claim(self, conn, project, ticket, started, working):
        store.tickets.transition(conn, ticket, "in_flight")
        run = store.claim(conn, project, ticket, now=started)
        self.set_clocks(conn, run, working, None)
        return run

    def test_a_failed_and_requeued_run_joins_its_tickets_chain(self):
        now = int(time() * 1000)
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project, ticket = self.open_ticket(conn, 5)
            first = self.claim(conn, project, ticket, now - 30 * MIN, 9 * MIN)
            finish_run(conn, first, "failed", "tests red", now=now - 20 * MIN)
            store.requeue(conn, ticket, "retry")
            second = self.claim(conn, project, ticket, now - 10 * MIN, 4 * MIN)
            finish_run(conn, second, "merged", now=now - MIN)
        finally:
            conn.close()
        self.start()

        _, _, body = self.request("GET", "/shipped")

        [row] = [r for r in body["rows"] if r["ticket"] == "KO-5"]
        self.assertEqual((row["id"], row["run_count"], row["agent_ms"]),
                         (second, 2, 13 * MIN))

    def test_a_single_run_ticket_reports_its_own_figures(self):
        now = int(time() * 1000)
        started, ended = now - 20 * MIN, now - 2 * MIN
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project, ticket = self.open_ticket(conn, 6)
            run = store.claim(conn, project, ticket, now=started)
            finding = dict(path="a.py", severity="p2", message="x")
            for n, findings in enumerate(([finding] * 2, []), start=1):
                store.record_review_round(
                    conn, run, n, "changes_requested" if findings else "pass",
                    "reviewer", started_at=started + n * MIN,
                    ended_at=started + n * MIN + SEC, findings=findings)
            finish_run(conn, run, "merged", now=ended)
            conn.execute("UPDATE runs SET workingMs = ?, verifyMs = ?,"
                         " workStartedAt = NULL, verifyStartedAt = NULL"
                         " WHERE id = ?", (11 * MIN, 3 * MIN, run))
            conn.commit()
        finally:
            conn.close()
        self.start()

        _, _, body = self.request("GET", "/shipped")

        [row] = [r for r in body["rows"] if r["ticket"] == "KO-6"]
        self.assertEqual(
            {key: row[key] for key in (
                "id", "started_ms", "wall_min", "working_ms", "agent_ms",
                "verify_ms", "rounds", "findings", "run_count")},
            {"id": run, "started_ms": started, "wall_min": 18.0,
             "working_ms": 11 * MIN, "agent_ms": 8 * MIN,
             "verify_ms": 3 * MIN, "rounds": 2, "findings": 2,
             "run_count": 1})

    def test_pages_hold_each_closing_run_once_newest_first(self):
        now = int(time() * 1000)
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            firsts, closing = [], []
            for n in (1, 2, 3):
                project, ticket = self.open_ticket(conn, 20 + n)
                start = now - (40 - 10 * n) * MIN
                first = self.claim(conn, project, ticket, start, MIN)
                finish_run(conn, first, "failed", "red", now=start + MIN)
                store.requeue(conn, ticket, "retry")
                second = self.claim(conn, project, ticket, start + 2 * MIN, MIN)
                finish_run(conn, second, "merged", now=start + 3 * MIN)
                firsts.append(first)
                closing.append(second)
        finally:
            conn.close()
        self.start()

        for outcome in ("", "&outcome=all"):
            with self.subTest(outcome=outcome):
                _, _, first_page = self.request(
                    "GET", f"/shipped?limit=2{outcome}")
                _, _, second_page = self.request(
                    "GET", f"/shipped?limit=2{outcome}"
                           f"&before={first_page['next_before']}")

                pages = [r["id"] for r in first_page["rows"]
                         + second_page["rows"]]
                self.assertEqual(pages, closing[::-1])
                self.assertFalse(set(pages) & set(firsts))
                self.assertIsNone(second_page["next_before"])

    def test_a_live_run_after_a_send_back_reports_its_chain(self):
        now = int(time() * 1000)
        started = now - 40 * MIN
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project, ticket = self.open_ticket(conn, 75)
            first = self.claim(conn, project, ticket, started, 0)
            park_run(conn, first, "awaiting_merge_approval",
                     now=started + 10 * MIN,
                     pr_url="https://example.test/pull/75")
            store.tickets.transition(conn, ticket, "blocked_on_operator")
            store.babysit(conn, ticket, "sent back", now=started + 10 * MIN)
            self.set_clocks(conn, first, 6 * MIN, MIN)
            live = self.claim(conn, project, ticket, now - 3 * MIN, 0)
            store.set_phase(conn, live, "working", now=now - 3 * MIN)
            conn.execute("UPDATE runs SET workingMs = ?, verifyMs = 0,"
                         " workStartedAt = ? WHERE id = ?",
                         (30 * SEC, now - MIN, live))
            conn.commit()
        finally:
            conn.close()

        code, body = holophyte.serve.server.status(
            Project.locate(self.target), now=now)

        self.assertEqual(code, 200)
        [row] = body["runs"]
        self.assertEqual(
            {key: row[key] for key in (
                "id", "started_ms", "elapsed_ms", "agent_ms", "run_count")},
            {"id": live, "started_ms": started, "elapsed_ms": now - started,
             "agent_ms": 5 * MIN + 30 * SEC + MIN, "run_count": 2})
