"""Operator close-out for work that landed outside the factory (KO-451)."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import holophyte.cli
import store
import store.tickets
from holophyte.board import lease_label
from holophyte.runs import open_store
from holophyte.target import Target

URL = "https://example.org/project/pull/122"


class CloseFlagTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        env = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(root / "home")})
        env.start()
        self.addCleanup(env.stop)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.target = Target.locate(self.repo)
        self.conn = open_store(self.target)
        self.addCleanup(self.conn.close)
        project = store.tickets.ensure_project(self.conn, "team", self.repo)
        self.ticket = store.tickets.mirror_ticket(
            self.conn, project, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="external landing",
            acceptance_criteria=["The external landing is recorded"],
            verification_commands=["echo ok"], time_box_ms=60000)
        store.tickets.transition(self.conn, self.ticket, "in_flight")
        self.run = store.claim(self.conn, project, self.ticket)
        for phase in ("working", "verifying", "reviewing", "merge_gate"):
            store.set_phase(self.conn, self.run, phase)
        self.board = Mock()

    def cli(self, *args):
        out = io.StringIO()
        with patch("holophyte.cli.board_config", return_value=("project", "team")), \
                patch("holophyte.cli.LinearProvider", return_value=self.board), \
                contextlib.redirect_stdout(out):
            holophyte.cli.cli([str(self.repo), *args])
        return out.getvalue()

    def end(self, outcome="rejected"):
        store.release(self.conn, self.run, outcome)
        store.tickets.transition(self.conn, self.ticket, "blocked_on_operator")
        self.conn.execute(
            "UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
            ("Rejected pull request: how should this proceed?", self.ticket))
        self.conn.commit()

    def test_close_records_external_landing_and_walks_board(self):
        self.end()
        out = self.cli("--close", "KO-1", "--landed", URL)
        self.assertIn("KO-1", out)
        self.assertIn(URL, out)
        self.assertEqual(self.conn.execute(
            'SELECT runId, action FROM interventions'
            " WHERE action != 'migrate'").fetchall(),
            [(self.run, "close_out")])
        self.assertIn(URL, str(self.conn.execute(
            "SELECT * FROM runEvents WHERE runId = ?", (self.run,)).fetchall()))
        self.assertEqual(self.conn.execute(
            "SELECT status FROM tickets WHERE id = ?", (self.ticket,)).fetchone(),
            ("merged",))
        self.assertEqual(self.conn.execute(
            "SELECT outcome, mergeSha FROM runs WHERE id = ?", (self.run,))
            .fetchone(), ("rejected", None))
        self.board.set_state.assert_called_once_with("issue-1", "Done")
        self.board.unlabel_issue.assert_called_once_with(
            "issue-1", lease_label(self.target))
        self.board.comment.assert_called_once()
        issue, comment = self.board.comment.call_args.args
        self.assertEqual(issue, "issue-1")
        self.assertIn(URL, comment)
        self.assertIn("no factory merge", comment)

    def test_close_projects_one_ledger_entry_to_board(self):
        self.end()
        before = self.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        self.cli("--close", "KO-1", "--landed", URL)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM ledger").fetchone()[0], before + 1)
        kind, text = self.conn.execute(
            "SELECT kind, text FROM ledger ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(kind, "intervention")
        self.assertIn(URL, text)
        self.board.comment.assert_called_once()
        self.assertIn(text, self.board.comment.call_args.args[1])

    def test_close_clears_resolved_operator_question(self):
        self.end()
        self.cli("--close", "KO-1", "--landed", URL)
        self.assertEqual(self.conn.execute(
            "SELECT status, blockedQuestion FROM tickets WHERE id = ?",
            (self.ticket,)).fetchone(), ("merged", None))

    def test_refusals_leave_store_and_board_unchanged(self):
        for case in ("live run", "parked", "merged", "unknown"):
            with self.subTest(case=case):
                if case == "parked":
                    store.park(self.conn, self.run, "awaiting_merge_approval",
                               candidate_sha="a" * 40)
                elif case == "merged":
                    self.end()
                    store.walk_ticket(self.conn, self.ticket, "merged")
                before = list(self.conn.iterdump())
                identifier = "KO-999" if case == "unknown" else "KO-1"
                with self.assertRaises(SystemExit) as raised:
                    self.cli("--close", identifier, "--landed", URL)
                self.assertIn(identifier, str(raised.exception))
                reason = {"parked": "live run", "unknown": "no such ticket"}.get(
                    case, case)
                self.assertIn(reason, str(raised.exception))
                self.assertEqual(list(self.conn.iterdump()), before)
                self.assertEqual(self.conn.execute(
                    'SELECT COUNT(*) FROM interventions'
                    " WHERE action != 'migrate'").fetchone(), (0,))
                self.assertEqual(self.board.mock_calls, [])

    def test_walk_failure_rolls_back_intervention_before_board_calls(self):
        self.end()
        before = list(self.conn.iterdump())
        with patch.object(store, "walk_ticket",
                          side_effect=RuntimeError("walk failed")):
            with self.assertRaisesRegex(RuntimeError, "walk failed"):
                self.cli("--close", "KO-1", "--landed", URL)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual(self.board.mock_calls, [])

    def test_optional_note_is_recorded_for_a_failed_run(self):
        self.end("failed")
        self.cli("--close", "KO-1", "--landed", URL,
                 "--note", "maintainer applied the patch")
        events = str(self.conn.execute(
            "SELECT * FROM runEvents WHERE runId = ?", (self.run,)).fetchall())
        self.assertIn("maintainer applied the patch", events)
        self.assertIn(URL, events)
        self.board.set_state.assert_called_once_with("issue-1", "Done")

    def test_landed_is_required_before_opening_store(self):
        err = io.StringIO()
        with patch("holophyte.operator._operator_store") as opened, \
                contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                self.cli("--close", "KO-1")
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--landed", err.getvalue())
        opened.assert_not_called()
