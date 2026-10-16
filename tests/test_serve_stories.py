"""`GET /stories/KEY`: one story's children with their in-story dependency
edges, its witnesses' latest verdicts and its decisions, over the socket.

Run: python3 -m unittest discover -s tests -p 'test_serve_stories.py' -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from time import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serve_fixture import MIN, ServeTestCase  # noqa: E402 - after the insert

import store  # noqa: E402 - after the sys.path insert above
import store.stories  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from tests.phase_fixture import finish_run  # noqa: E402 - after the insert

SHA_A = "a" * 40
SHA_B = "b" * 40
WITNESSES = [
    {"key": key, "criterion": f"criterion {key}", "file": f"tests/w{key}.py",
     "command": f"python3 tests/w{key}.py", "source": "assert True\n"}
    for key in ("W1", "W2")]


class StoryDetailTests(ServeTestCase):
    def seed_story(self):
        """Story DEMO-20 over DEMO-21 (merged), DEMO-22 (live run, waits on
        DEMO-21) and DEMO-23 (backlog, waits on DEMO-22 and on DEMO-30,
        outside the story), each child filed and claimed through the
        store's own writers."""
        self.now = int(time() * 1000)
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "team-1", self.target)

            def mirror(n, depends_on=(), column=None):
                return store.tickets.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{n}",
                    linear_identifier=f"DEMO-{n}", title=f"ticket {n}",
                    acceptance_criteria=[f"Given {n}, then it is worked"],
                    verification_commands=["echo ok"], time_box_ms=n * MIN,
                    depends_on=[f"issue-{dep}" for dep in depends_on],
                    board_column=column)

            parent = mirror(20)
            mirror(30)
            merged = mirror(21)
            flying = mirror(22, depends_on=[21])
            waiting = mirror(23, depends_on=[22, 30], column="backlog")

            store.tickets.transition(conn, merged, "in_flight")
            run = store.claim(conn, project, merged, now=self.now - 50 * MIN)
            self.merged_at = self.now - 40 * MIN
            finish_run(conn, run, "merged", now=self.merged_at,
                       merge_sha=SHA_A)
            store.tickets.transition(conn, merged, "merged")
            store.tickets.transition(conn, flying, "in_flight")
            self.live_run = store.claim(conn, project, flying,
                                        now=self.now - 10 * MIN)

            store.stories.file_story(
                conn, parent, WITNESSES,
                [(merged, "scaffolding", ()), (flying, "advances", ("W1",)),
                 (waiting, "completes", ("W1", "W2"))])
            (revision,) = conn.execute("SELECT revision FROM tickets"
                                       " WHERE id = ?", (parent,)).fetchone()
            store.stories.approve_story(conn, parent, revision, "maintainer",
                                        "approved", now=self.now - 45 * MIN)

            self.green_at, self.red_at = self.now - 30 * MIN, self.now - 20 * MIN
            store.stories.record_witness_result(
                conn, parent, "W1", SHA_A, "green", "loop", now=self.green_at)
            store.stories.record_witness_result(
                conn, parent, "W1", SHA_B, "red", "loop", red_kind="assert",
                now=self.red_at)

            self.asked_at = self.now - 15 * MIN
            self.answered_at = self.now - 12 * MIN
            self.reopened_at = self.now - 5 * MIN
            self.first = store.stories.park_story(
                conn, parent, "regressed", "W1 went red; replan?",
                ["replan", "abandon"], "replan", ticket_id=flying,
                now=self.asked_at)
            store.stories.answer_decision(conn, self.first, "replan",
                                          "maintainer", "replanning",
                                          now=self.answered_at)
            self.second = store.stories.park_story(
                conn, parent, "unmet", "W2 is still unmet; wait?",
                ["wait", "abandon"], "wait", now=self.reopened_at)
        finally:
            conn.close()

    def test_children_come_in_plan_order_with_their_in_story_edges(self):
        self.seed_story()
        self.start()

        code, _, body = self.request("GET", "/stories/DEMO-20")

        self.assertEqual(code, 200)
        self.assertEqual((body["ticket"], body["title"], body["state"]),
                         ("DEMO-20", "ticket 20", "parked"))
        children = body["children"]
        self.assertEqual([child["ticket"] for child in children],
                         ["DEMO-21", "DEMO-22", "DEMO-23"])
        merged, flying, waiting = children
        self.assertEqual((merged["merged_ms"], merged["run"], merged["status"]),
                         (self.merged_at, None, "merged"))
        self.assertEqual((flying["run"], flying["depends_on"],
                          flying["merged_ms"]),
                         (self.live_run, ["DEMO-21"], None))
        self.assertEqual((waiting["column"], waiting["depends_on"],
                          waiting["witness_keys"], waiting["role"],
                          waiting["time_box_ms"]),
                         ("backlog", ["DEMO-22"], ["W1", "W2"], "completes",
                          23 * MIN))

    def test_a_witness_reads_its_verdict_at_the_newest_sha_or_nulls(self):
        self.seed_story()
        self.start()

        _, _, body = self.request("GET", "/stories/DEMO-20")

        self.assertEqual(body["witnesses"], [
            {"key": "W1", "criterion": "criterion W1", "verdict": "red",
             "main_sha": SHA_B, "at": self.red_at},
            {"key": "W2", "criterion": "criterion W2", "verdict": None,
             "main_sha": None, "at": None}])

    def test_decisions_come_oldest_first_answered_or_open(self):
        self.seed_story()
        self.start()

        _, _, body = self.request("GET", "/stories/DEMO-20")

        answered, open_ = body["decisions"]
        self.assertEqual(
            (answered["id"], answered["ticket"], answered["kind"],
             answered["options"], answered["default_option"],
             answered["answer"], answered["answered_by"],
             answered["answered_ms"], answered["at"]),
            (self.first, "DEMO-22", "regressed", ["replan", "abandon"],
             "replan", "replan", "maintainer", self.answered_at,
             self.asked_at))
        self.assertEqual(
            (open_["id"], open_["ticket"], open_["question"], open_["answer"],
             open_["answered_by"], open_["answered_ms"], open_["at"]),
            (self.second, None, "W2 is still unmet; wait?", None, None, None,
             self.reopened_at))

    def test_a_child_or_an_unmirrored_key_is_404_and_no_store_is_503(self):
        self.seed_story()
        self.start()

        for key in ("DEMO-21", "DEMO-99"):
            code, _, body = self.request("GET", f"/stories/{key}")
            self.assertEqual((code, body), (404, {}), key)

    def test_a_project_with_no_store_answers_503(self):
        self.start()

        code, _, body = self.request("GET", "/stories/DEMO-20")

        self.assertEqual(code, 503)
        self.assertEqual(body["error"], "no store")
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
