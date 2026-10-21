import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import store
import store.board
import store.steer_notes
import store.tickets
from holophyte.loop import adjudicate, runs
from holophyte.loop.gates import RunFailure
from tests.test_ticket_criteria_rows import COMMANDS, ticket_body

CRITERIA = ["the list shows every entry"]
V1 = "python3 -m unittest discover -s tests -p 'test_a.py'"
V2 = "python3 -m unittest discover -s tests -p 'test_b.py'"
CASE = ("import unittest\n\nclass Case(unittest.TestCase):\n"
        "    def test_case(self):\n        self.assertTrue({})\n")

PROJECT = SimpleNamespace(config=lambda: {}, config_path="holophyte.toml")


def body(verify):
    return ticket_body(CRITERIA).replace(COMMANDS, verify)


class SteerVerifyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "tests").mkdir()
        (self.root / "tests/test_a.py").write_text(CASE.format(False))
        (self.root / "tests/test_b.py").write_text(CASE.format(True))
        for args in (("init", "-q", "-b", "main"), ("add", "tests"),
                     ("-c", "user.name=t", "-c", "user.email=t@example.test",
                      "commit", "-qm", "base")):
            subprocess.run(["git", *args], cwd=self.root, check=True)
        self.conn = store.open(self.root / "store.db")
        self.addCleanup(self.conn.close)
        project = store.tickets.ensure_project(
            self.conn, "native-team", directory.name)
        key = store.board.file_ticket(self.conn, project, "NAT", body(V1))
        ticket = self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?", (key,)
        ).fetchone()[0]
        store.tickets.transition(self.conn, ticket, "in_flight")
        self.run_id = store.claim(self.conn, project, ticket)
        edited = int(time.time() * 1000) + 1000
        store.board.edit_ticket(self.conn, project, key, body(V2),
                                expected_revision=1, now=edited)
        store.steer_notes.steer(
            self.conn, ticket, "Meet revision 2's contract", "maintainer",
            now=edited + 1000)
        store.set_phase(self.conn, self.run_id, "working")

    def test_terminal_adjudication_verifies_with_the_adopted_block(self):
        with patch.object(adjudicate, "agent",
                          return_value="VERDICT: FAIL") as agent, \
                patch.object(runs, "agent_route", return_value="adjudicator"):
            with self.assertRaises(RunFailure) as failed:
                adjudicate._terminal_adjudication(
                    PROJECT, self.conn, self.run_id, None, "NAT-1", {}, "main",
                    self.root, 60, "base", "head", body(V1), V1, [], 1,
                    criteria=CRITERIA)
        self.assertNotIn("verify failed", str(failed.exception))
        agent.assert_called_once()
        self.assertIn(f"Amended verify command(s):\n{V2}",
                      agent.call_args.args[2])
        (results,) = self.conn.execute(
            "SELECT verificationResults FROM reviewRounds WHERE runId = ?",
            (self.run_id,)).fetchone()
        (ticket,) = [row for row in json.loads(results)
                     if row["source"] == "ticket"]
        self.assertEqual(ticket["command"], V2)


if __name__ == "__main__":
    unittest.main()
