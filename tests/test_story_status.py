"""`factory.py TARGET --status [--json]` shows each open story: its state,
generation, child counts, latest witness verdicts, errors, decisions and age.

Run: python3 -m unittest discover -s tests -p 'test_story_status.py' -v
"""
from __future__ import annotations

import io
import json
import sys
from unittest.mock import patch

import holophyte.cli.cli
import holophyte.cli.status
import store.board
from store.stories import (
    approve_story,
    close_story,
    file_story,
    park_story,
    record_witness_result,
)
from store.tickets import walk_ticket
from tests.sweep_fixture import T0, SweepTestCase, Tripwire, no_network
from tests.test_store_board import body

HOUR = 3600 * 1000
WITNESSES = [
    {"key": key, "criterion": f"outcome {key}", "file": f"tests/test_{key}.py",
     "command": f"python3 -m unittest tests.test_{key}", "source": "pass\n"}
    for key in ("W1", "W2")]


class StoryStatusTests(SweepTestCase):
    def file(self, title):
        identifier = store.board.file_ticket(
            self.conn, self.project_id, "NAT", body(title), column="backlog",
            now=T0)
        return self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?",
            (identifier,)).fetchone()[0]

    def story(self, title, children):
        parent = self.file(title)
        ids = [self.file(f"{title} step {n}") for n in range(1, children + 1)]
        file_story(self.conn, parent, WITNESSES,
                   [(child, "advances", ("W1",)) for child in ids[:-1]]
                   + [(ids[-1], "completes", ("W1", "W2"))])
        (revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?", (parent,)).fetchone()
        approve_story(self.conn, parent, revision, "operator", "go")
        return parent, ids

    def verdict(self, parent, key, sha, verdict):
        record_witness_result(self.conn, parent, key, sha, verdict, "loop",
                              red_kind="assert" if verdict == "red" else None)

    def status(self, *flags, at=T0 + 2 * 24 * HOUR + 5 * HOUR):
        holophyte.cli.cli.eager_import()
        out = io.StringIO()
        with patch.dict(sys.modules,
                        {"linear_provider": Tripwire("linear_provider")}), \
                no_network(), patch.object(sys, "stdout", out), \
                patch.object(holophyte.cli.status, "time", lambda: at / 1000):
            code = holophyte.cli.cli.cli([str(self.target), "--status", *flags])
        self.assertEqual(code, 0)
        return out.getvalue()

    def guest_checkout(self):
        parent, children = self.story("Guest checkout", 4)
        for child in children[:3]:
            walk_ticket(self.conn, child, "merged")
        walk_ticket(self.conn, children[3], "in_flight")
        self.a_run(ticket=children[3])
        self.verdict(parent, "W1", "a1b2c3", "red")
        self.verdict(parent, "W2", "a1b2c3", "error")
        self.verdict(parent, "W1", "d4e5f6", "green")
        self.verdict(parent, "W2", "d4e5f6", "absent")

    def test_text_shows_an_approved_storys_two_lines(self):
        self.guest_checkout()
        lines = self.status().splitlines()
        [first] = [n for n, line in enumerate(lines)
                   if line.startswith("NAT-1 ")]
        self.assertTrue(lines[first].startswith(
            'NAT-1 story "Guest checkout"  approved gen 3  children 3/4 merged,'
            " 1 running, 0 frontier, 0 waiting"), lines[first])
        self.assertIn("witnesses W1 green  W2 absent @d4e5f6  errors 0"
                      "  decisions 0", lines[first + 1])
        self.assertTrue(lines[first + 1].endswith("age 2d 5h"),
                        lines[first + 1])

    def test_json_carries_the_story_facts(self):
        self.guest_checkout()
        [fact] = json.loads(self.status("--json"))["stories"]
        self.assertEqual(
            {key: fact[key] for key in ("ticket", "title", "state",
                                        "generation", "children", "witnesses",
                                        "commit", "errors", "decisions")},
            {"ticket": "NAT-1", "title": "Guest checkout", "state": "approved",
             "generation": 3,
             "children": {"total": 4, "merged": 3, "running": 1,
                          "frontier": 0, "waiting": 0},
             "witnesses": [{"key": "W1", "verdict": "green"},
                           {"key": "W2", "verdict": "absent"}],
             "commit": "d4e5f6", "errors": 0, "decisions": 0})

    def test_parked_story_counts_its_error_and_decision_and_closed_is_hidden(self):
        closed, [child] = self.story("Old export", 1)
        walk_ticket(self.conn, child, "merged")
        close_story(self.conn, closed, "0a0b0c", "done")
        parked, _ = self.story("Guest checkout", 2)
        self.verdict(parked, "W1", "0a0b0c", "green")
        self.verdict(parked, "W1", "c0ffee", "error")
        self.verdict(parked, "W2", "c0ffee", "green")
        park_story(self.conn, parked, "unmet", "W1 errors; rerun it?",
                   ["rerun", "abandon"], "rerun")
        lines = self.status().splitlines()
        self.assertEqual([line for line in lines if " story " in line], [
            'NAT-3 story "Guest checkout"  parked gen 0  children 0/2 merged,'
            " 0 running, 0 frontier, 2 waiting"])
        [second] = [line for line in lines if "witnesses" in line]
        self.assertIn("W1 error  W2 green @c0ffee  errors 1  decisions 1",
                      second)

    def test_a_story_filed_after_now_is_age_zero_in_text_and_json(self):
        self.guest_checkout()
        before_filing = T0 - 3 * HOUR
        [fact] = json.loads(self.status("--json", at=before_filing))["stories"]
        self.assertEqual(fact["age_s"], 0)
        [second] = [line for line in self.status(at=before_filing).splitlines()
                    if "witnesses" in line]
        self.assertTrue(second.endswith("age 0d 0h"), second)
