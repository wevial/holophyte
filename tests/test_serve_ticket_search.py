"""`GET /tickets?q=TEXT` names the tickets whose identifier, title, labels or
body hold the text, ignoring case, closed tickets included.

A real store is seeded and read back from a real daemon on a loopback
ephemeral port.

Run: python3 -m unittest discover -s tests -p 'test_serve_ticket_search.py' -v
"""
import unittest

import store
import store.tickets
from tests.serve_fixture import ServeTestCase


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


if __name__ == "__main__":
    unittest.main()
