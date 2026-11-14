"""`GET /tickets?q=TEXT` names the tickets whose identifier, title, labels or
body hold the text, ignoring case, closed tickets included. `GET /search?q=TEXT`
answers the matching tickets, runs, scouts and review findings in four groups,
at a project daemon and at a host daemon's `/projects/NAME/search`.

A real store is seeded and read back from a real daemon on a loopback
ephemeral port.

Run: python3 -m unittest discover -s tests -p 'test_serve_ticket_search.py' -v
"""
import sqlite3
import threading
import unittest

import holophyte.serve.server
import store
import store.scouts
import store.tickets
from holophyte.config.project import Project
from tests.phase_fixture import finish_run
from tests.serve_fixture import ServeTestCase
from tests.test_serve_host import HostServeCase

T0 = 1_788_478_203_000


def mirror(conn, project, key, title, body="Plain text"):
    return store.tickets.mirror_ticket(
        conn, project, linear_issue_id=f"issue-{key}", linear_identifier=key,
        title=title, body=body, acceptance_criteria=["Given it, then done"],
        verification_commands=["echo ok"])


def seed_one_of_each(conn, project):
    """KO-21's title holds "Quokka", so it and its merged run match; a
    scout's ask holds "qUoKkA" and a round-2 finding's message "quokka"."""
    mirror(conn, project, "KO-20", "Unrelated", body="nothing")
    ticket = mirror(conn, project, "KO-21", "The Quokka trail")
    store.tickets.transition(conn, ticket, "in_flight")
    run = store.claim(conn, project, ticket, now=T0)
    store.record_review_round(conn, run, 1, "changes_requested", "reviewer-model",
                              findings=[{"path": "a.py", "severity": "p2",
                                         "message": "no match here"}])
    store.record_review_round(
        conn, run, 2, "pass", "reviewer-model",
        findings=[{"path": "holophyte/loop/gates.py", "line": 212,
                   "severity": "p1", "message": "the quokka turn is skipped"}])
    finish_run(conn, run, "merged", now=T0 + 60_000)
    store.tickets.transition(conn, ticket, "merged")
    store.scouts.create_scout(conn, project, "Where does a qUoKkA sleep?",
                              "research", 30)
    store.scouts.create_scout(conn, project, "Unrelated ask", "research", 30)
    return run


ONE_OF_EACH = {
    "tickets": [{"ticket": "KO-21", "title": "The Quokka trail",
                 "status": "merged"}],
    "scouts": [{"id": 1, "title": "Where does a qUoKkA sleep?",
                "phase": "queued"}],
    "findings": [{"run": 1, "round": 2, "severity": "p1",
                  "message": "the quokka turn is skipped",
                  "path": "holophyte/loop/gates.py", "line": 212}]}


class TicketSearchTests(ServeTestCase):
    def setUp(self):
        super().setUp()
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            for key, title, body, labels, ends in (
                    ("KO-1", "Fix the Board lane", "Plain text", [], None),
                    ("KO-2", "Copy edit", "A quokka in the body", ["ux"], None),
                    ("KO-3", "Merged one", "AN EARLIER QUOKKA", [], "merged"),
                    ("KO-4", "Dropped one", "Nothing here", ["infra"],
                     "abandoned")):
                ticket = store.tickets.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{key}",
                    linear_identifier=key, title=title, body=body,
                    labels=labels, acceptance_criteria=["Given it, then done"],
                    verification_commands=["echo ok"])
                if ends is not None:
                    store.tickets.transition(conn, ticket, "in_flight")
                    store.tickets.transition(conn, ticket, ends)
        finally:
            conn.close()
        self.start()

    def test_a_search_matches_body_title_label_and_key_ignoring_case(self):
        for query, keys in (("quokka", ["KO-2", "KO-3"]), ("board", ["KO-1"]),
                            ("UX", ["KO-2"]), ("ko-1", ["KO-1"])):
            with self.subTest(q=query):
                code, _, body = self.request("GET", f"/tickets?q={query}")
                self.assertEqual((code, body), (200, {"q": query,
                                                      "tickets": keys}))

    def test_a_missing_or_blank_q_is_refused(self):
        for path in ("/tickets?q=", "/tickets"):
            with self.subTest(path=path):
                code, _, body = self.request("GET", path)
                self.assertEqual((code, body), (400, {"error": "q is required"}))


class SearchTests(ServeTestCase):
    def seeded(self, seed):
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            result = seed(conn, store.tickets.ensure_project(
                conn, "team-1", self.target))
        finally:
            conn.close()
        self.start()
        return result

    def test_each_group_lists_its_one_match_ignoring_case(self):
        run = self.seeded(seed_one_of_each)

        code, _, body = self.request("GET", "/search?q=QUOKKA")

        self.assertEqual(code, 200)
        self.assertEqual(body, {
            "q": "QUOKKA", "limit": 20, **ONE_OF_EACH,
            "runs": [{"id": run, "ticket": "KO-21", "title": "The Quokka trail",
                      "phase": "done", "outcome": "merged", "started_ms": T0}]})

    def test_runs_are_newest_start_first_and_limit_caps_every_group(self):
        def seed(conn, project):
            runs = {}
            for n, started in ((1, T0 + 2), (2, T0), (3, T0 + 1)):
                ticket = mirror(conn, project, f"KO-{n}", f"quokka {n}")
                store.tickets.transition(conn, ticket, "in_flight")
                runs[started] = store.claim(conn, project, ticket, now=started)
                store.record_review_round(
                    conn, runs[started], 1, "changes_requested", "reviewer-model",
                    findings=[{"path": "a.py", "severity": "p2",
                               "message": f"quokka {n}"}])
                store.scouts.create_scout(conn, project, f"quokka {n}",
                                          "research", 30)
            return [runs[T0 + 2], runs[T0 + 1], runs[T0]]

        newest_first = self.seeded(seed)

        _, _, body = self.request("GET", "/search?q=quokka")
        self.assertEqual([run["id"] for run in body["runs"]], newest_first)
        _, _, body = self.request("GET", "/search?q=quokka&limit=2")
        self.assertEqual({group: len(body[group]) for group in
                          ("tickets", "runs", "scouts", "findings")},
                         {"tickets": 2, "runs": 2, "scouts": 2, "findings": 2})
        self.assertEqual([run["id"] for run in body["runs"]], newest_first[:2])

    def test_an_all_digit_q_finds_the_run_with_that_id(self):
        def seed(conn, project):
            for n in (1, 2):
                ticket = mirror(conn, project, f"KO-{n}", f"ticket {n}")
                store.tickets.transition(conn, ticket, "in_flight")
                store.claim(conn, project, ticket, now=T0 + n)

        self.seeded(seed)
        raw = sqlite3.connect(str(self.db))
        try:
            raw.execute("UPDATE runs SET id = 403 WHERE id = 2")
            raw.commit()
        finally:
            raw.close()

        _, _, body = self.request("GET", "/search?q=403")

        self.assertEqual([(run["id"], run["title"]) for run in body["runs"]],
                         [(403, "ticket 2")])

    def test_findings_are_run_details_matched_on_their_stored_text(self):
        def seed(conn, project):
            ticket = mirror(conn, project, "KO-1", "ticket 1")
            store.tickets.transition(conn, ticket, "in_flight")
            run = store.claim(conn, project, ticket, now=T0)
            store.record_review_round(
                conn, run, 1, "changes_requested", "github:coderabbitai",
                findings=[dict(kind="instruction", path="app.py", line=1,
                               severity="nit", author="coderabbitai",
                               request="Handle empty tokens",
                               message="Handle empty tokens"),
                          dict(kind="instruction", path="retry.py", line=3,
                               severity="nit", author="coderabbitai",
                               request="**Handle retries**\n\nThe empty"
                                       " tokens retry is skipped.",
                               message="**Handle retries**\n\nThe empty"
                                       " tokens retry is skipped."),
                          dict(kind="instruction", path="app.py", line=2,
                               severity="nit", author="operator",
                               request="Rename the empty tokens helper",
                               message="Rename the empty tokens helper")])
            return run

        run = self.seeded(seed)

        _, _, detail = self.request("GET", f"/runs/{run}")
        _, _, body = self.request("GET", "/search?q=empty%20tokens")

        served = [f["message"] for f in detail["rounds"][0]["findings"]]
        self.assertEqual(served, ["Handle empty tokens", "Handle retries"])
        self.assertEqual([f["message"] for f in body["findings"]], served)

    def test_a_missing_or_blank_q_is_refused(self):
        self.seeded(lambda conn, project: None)
        for path in ("/search", "/search?q=", "/search?q=%20%20"):
            with self.subTest(path=path):
                code, _, body = self.request("GET", path)
                self.assertEqual((code, body), (400, {"error": "q is required"}))


class HostSearchTests(HostServeCase):
    def test_a_host_daemon_answers_the_project_daemons_body(self):
        path = self.paths["alpha"]
        conn = store.open(str(Project.locate(path).store_path))
        try:
            seed_one_of_each(conn, store.tickets.ensure_project(
                conn, "team-alpha", path))
        finally:
            conn.close()
        self.start()
        server = holophyte.serve.server.make_server(
            Project.locate(path), "127.0.0.1", 0,
            console_dir=self.root / "no-console", token=None)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        host_port, self.port = self.port, server.server_address[1]
        own = self.request("GET", "/search?q=quokka")
        self.port = host_port

        hosted = self.request("GET", "/projects/alpha/search?q=quokka")

        self.assertEqual(hosted, own)
        self.assertEqual(hosted[0], 200)
        self.assertEqual(
            {group: len(hosted[1][group]) for group in ONE_OF_EACH},
            {"tickets": 1, "scouts": 1, "findings": 1})


if __name__ == "__main__":
    unittest.main()
