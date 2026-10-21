import contextlib
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
from holophyte.loop import adjudicate, merge_gate, review_round, runs, steering
from holophyte.loop.gates import MergeParked, RunFailure
from holophyte.review import reproduce
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
        self.ticket = ticket
        self.edited = int(time.time() * 1000) + 1000
        store.board.edit_ticket(self.conn, project, key, body(V2),
                                expected_revision=1, now=self.edited)
        store.set_phase(self.conn, self.run_id, "working")

    def steer(self, hint=False):
        store.steer_notes.steer(
            self.conn, self.ticket, "Meet revision 2's contract", "maintainer",
            hint=hint, now=self.edited + (1000 if hint else 2000))

    def ticket_command(self):
        (results,) = self.conn.execute(
            "SELECT verificationResults FROM reviewRounds WHERE runId = ?"
            " ORDER BY round DESC", (self.run_id,)).fetchone()
        (ticket,) = [row for row in json.loads(results)
                     if row["source"] == "ticket"]
        return ticket["command"]

    def test_adopted_verify_follows_only_an_amendment_onto_the_revision(self):
        self.assertEqual(steering.adopted_verify(self.conn, self.run_id, V1),
                         V1)
        self.steer(hint=True)
        self.assertEqual(steering.adopted_verify(self.conn, self.run_id, V1),
                         V1)
        self.steer()
        self.assertEqual(steering.adopted_verify(self.conn, self.run_id, V1),
                         V2)

    def test_terminal_adjudication_verifies_with_the_adopted_block(self):
        self.steer()
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
        self.assertEqual(self.ticket_command(), V2)

    def test_review_round_verifies_with_the_adopted_block(self):
        self.steer()
        steering.take(self.conn, self.run_id)
        with patch.object(review_round, "agent",
                          return_value="VERDICT: APPROVE"), \
                patch.object(runs, "agent_route", return_value="reviewer"), \
                patch("holophyte.agents.fix_session.fix_turn",
                      return_value=("nothing to fix", False)), \
                contextlib.suppress(RunFailure):
            review_round._review_rounds(
                PROJECT, self.conn, self.run_id, None, "NAT-1", "main",
                self.root, 60, "HEAD", "HEAD", body(V1), V1, [], CRITERIA, 10,
                1)
        self.assertEqual(self.ticket_command(), V2)

    def test_resumed_round_reverifies_a_checkpoint_saved_under_the_claimed_block(self):
        self.steer()
        steering.take(self.conn, self.run_id)
        saved = {"rnd": 1, "phase": "reviewing", "ok": False,
                 "out": "[verify] FAILED: test_a.py", "verify_cmd": V1}
        with patch.object(review_round, "agent",
                          return_value="VERDICT: APPROVE"), \
                patch.object(runs, "agent_route", return_value="reviewer"), \
                patch("holophyte.agents.fix_session.fix_turn",
                      return_value=("nothing to fix", False)), \
                contextlib.suppress(RunFailure):
            review_round._review_rounds(
                PROJECT, self.conn, self.run_id, None, "NAT-1", "main",
                self.root, 60, "HEAD", "HEAD", body(V1), V1, [], CRITERIA, 10,
                1, resume=saved)
        (results,) = self.conn.execute(
            "SELECT verificationResults FROM reviewRounds WHERE runId = ?",
            (self.run_id,)).fetchone()
        (ticket,) = [row for row in json.loads(results)
                     if row["source"] == "ticket"]
        self.assertEqual((ticket["command"], ticket["exitCode"]), (V2, 0))

    def test_rendering_a_rendered_ticket_keeps_one_multiline_verify_block(self):
        split = f"{V2}\n\n{V1}"
        key = self.conn.execute(
            "SELECT linearIdentifier, projectId FROM tickets WHERE id = ?",
            (self.ticket,)).fetchone()
        store.board.edit_ticket(self.conn, key[1], key[0], body(split),
                                expected_revision=2, now=self.edited + 500)
        self.steer()
        once = steering.reviewed_ticket(self.conn, self.run_id, body(V1))
        self.assertIn(f"Amended verify command(s):\n{split}", once)
        twice = steering.reviewed_ticket(self.conn, self.run_id, once)
        self.assertTrue(twice.endswith(f"Amended verify command(s):\n{split}"))
        self.assertEqual((twice.count(V1), twice.count(V2)),
                         (once.count(V1), once.count(V2)))

    def test_not_reproduced_evidence_check_verifies_with_the_adopted_block(self):
        self.steer()
        steering.take(self.conn, self.run_id)
        with patch.object(review_round, "agent",
                          return_value="VERDICT: PASS"), \
                patch.object(runs, "agent_route", return_value="adjudicator"), \
                self.assertRaises(MergeParked):
            reproduce.review_rounds(
                PROJECT, self.conn, self.run_id, None, "NAT-1", "main",
                self.root, 60, "HEAD", "HEAD", body(V1), V1, [], CRITERIA, 10,
                1)
        self.assertEqual(self.ticket_command(), V2)

    def test_resumed_evidence_check_reverifies_a_checkpoint_of_the_claimed_block(self):
        self.steer()
        steering.take(self.conn, self.run_id)
        saved = {"rnd": 1, "phase": "reviewing", "ok": False,
                 "out": "[verify] FAILED: test_a.py", "verify_cmd": V1}
        with patch.object(review_round, "agent",
                          return_value="VERDICT: PASS"), \
                patch.object(runs, "agent_route", return_value="adjudicator"), \
                self.assertRaises(MergeParked):
            reproduce.review_rounds(
                PROJECT, self.conn, self.run_id, None, "NAT-1", "main",
                self.root, 60, "HEAD", "HEAD", body(V1), V1, [], CRITERIA, 10,
                1, resume=saved)
        self.assertEqual(self.ticket_command(), V2)

    def test_second_evidence_round_follows_a_revision_back_to_the_claimed_block(self):
        self.steer()
        steering.take(self.conn, self.run_id)
        key, project = self.conn.execute(
            "SELECT linearIdentifier, projectId FROM tickets WHERE id = ?",
            (self.ticket,)).fetchone()

        def reply(_target, _role, prompt, *args, **kwargs):
            if "evidence checker" in prompt:
                return "VERDICT: FAIL"
            return "VERDICT: APPROVE"

        def fix(*args, **kwargs):
            if fix.calls:
                return "nothing to fix", False
            fix.calls += 1
            store.board.edit_ticket(
                self.conn, project, key,
                ticket_body(CRITERIA + ["the list is sorted"]).replace(
                    COMMANDS, V1),
                expected_revision=2, now=self.edited + 3000)
            store.steer_notes.steer(
                self.conn, self.ticket, "Meet revision 3's contract",
                "maintainer", now=self.edited + 4000)
            (self.root / "notes.txt").write_text("fix\n")
            for git in (("add", "notes.txt"),
                        ("-c", "user.name=t", "-c", "user.email=t@example.test",
                         "commit", "-qm", "fix")):
                subprocess.run(["git", *git], cwd=self.root, check=True)
            return f"fixed\n{reproduce.DECLARATION}", False
        fix.calls = 0

        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                              check=True, capture_output=True,
                              text=True).stdout.strip()
        with patch.object(review_round, "agent", side_effect=reply), \
                patch.object(runs, "agent_route", return_value="reviewer"), \
                patch("holophyte.agents.fix_session.fix_turn",
                      side_effect=fix), \
                patch("holophyte.agents.fix_session._session_turn",
                      return_value=("steered", False)) as steer_agent, \
                contextlib.suppress(RunFailure, MergeParked):
            reproduce.review_rounds(
                PROJECT, self.conn, self.run_id, None, "NAT-1", "main",
                self.root, 60, base, base, body(V1), V1, [], CRITERIA, 10,
                1)
        self.assertEqual(fix.calls, 1)
        steer_agent.assert_called_once()
        self.assertEqual(self.ticket_command(), V1)

    def test_merge_gate_verifies_with_the_adopted_block(self):
        self.steer()
        for phase in ("verifying", "reviewing"):
            store.set_phase(self.conn, self.run_id, phase)
        merge_gate._merge_gate(
            PROJECT, self.conn, self.run_id, None, "NAT-1", "issue", "main",
            self.root, 60, "HEAD", V1, [], body(V1), 10, sync_main=False)
        (question,) = self.conn.execute(
            "SELECT blockedQuestion FROM tickets WHERE id = ?",
            (self.ticket,)).fetchone()
        self.assertIsNone(question)


if __name__ == "__main__":
    unittest.main()
