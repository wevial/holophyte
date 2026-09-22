"""CLI send-back instructions and plain PR rechecks under human approval."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli
import store
import store.tickets
from holophyte import maintainer_notes
from holophyte.pr import PrState
from holophyte.target import Target
from tests.phase_fixture import park_run


class BabysitCliFixture:
    URL = "https://example.test/pull/7"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "repo"
        self.repo.mkdir()
        env = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(Path(tmp.name) / "home")})
        env.start()
        self.addCleanup(env.stop)
        self.target = Target.locate(self.repo)
        self.target.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.target.config_path.write_text(
            '[board]\nproject_id = "p-1"\nteam = "T"\n'
            '[merge]\nmode = "pr"\napprove = "human"\n')
        self.target.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = store.open(self.target.store_path, migrate="owner")
        self.addCleanup(self.conn.close)
        self.project = store.tickets.ensure_project(self.conn, "team-1", self.repo)
        self.ticket = store.tickets.mirror_ticket(
            self.conn, self.project, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="a ticket",
            acceptance_criteria=["The button has readable padding"],
            verification_commands=["echo ok"], time_box_ms=25 * 60 * 1000)
        store.tickets.transition(self.conn, self.ticket, "in_flight")
        self.run = store.claim(self.conn, self.project, self.ticket)
        park_run(self.conn, self.run, "awaiting_merge_approval", "merge?",
                   pr_url=self.URL, candidate_sha="a" * 40)
        store.tickets.transition(self.conn, self.ticket, "blocked_on_operator")

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                patch("getpass.getuser", return_value="operator"):
            holophyte.cli.cli([str(self.repo), "--babysit", "KO-1", *args])
        return out.getvalue().strip()

    def pending(self):
        store.tickets.transition(self.conn, self.ticket, "in_flight")
        resumed = store.claim(self.conn, self.project, self.ticket)
        return maintainer_notes.pending_state(
            self.conn, resumed, PrState((), "success", "a" * 40), self.URL)


class BabysitCliTests(BabysitCliFixture, unittest.TestCase):
    def test_custom_note_records_instruction_and_readies_ticket(self):
        out = self.cli("--note", "fix the padding")
        event_id, payload = self.conn.execute(
            "SELECT id, payload FROM runEvents WHERE runId = ?"
            " AND kind = 'operator_note'",
            (self.run,)).fetchone()
        self.assertEqual(json.loads(payload),
                         {"note": "fix the padding", "author": "operator"})
        self.assertEqual(self.conn.execute(
            "SELECT status FROM tickets WHERE id = ?", (self.ticket,)
        ).fetchone(), ("ready",))
        self.assertTrue(out.endswith(
            f"as a maintainer instruction (operator_note event {event_id})"), out)
        self.assertEqual(len(self.pending().threads), 1)

    def test_no_note_only_requests_another_look(self):
        self.assert_another_look()

    def test_explicit_default_only_requests_another_look(self):
        self.assert_another_look("--note", holophyte.cli.BABYSIT_DEFAULT_NOTE)

    def assert_another_look(self, *args):
        out = self.cli(*args)
        self.assertEqual(self.conn.execute(
            "SELECT id FROM runEvents WHERE kind = 'operator_note'").fetchall(), [])
        self.assertEqual(self.conn.execute(
            "SELECT action FROM interventions WHERE runId = ?", (self.run,)
        ).fetchall(), [("babysit",)])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM tickets WHERE id = ?", (self.ticket,)
        ).fetchone(), ("ready",))
        self.assertTrue(out.endswith("for another look"), out)
        self.assertEqual(self.pending().threads, ())


class CliMaintainerThreadTests(BabysitCliFixture, unittest.TestCase):
    def test_cli_note_becomes_a_pending_maintainer_instruction_after_resume(self):
        self.cli("--note", "fix the padding")
        state = self.pending()
        self.assertEqual(len(state.threads), 1)
        thread, = state.threads
        self.assertEqual(thread.author_kind, "maintainer")
        self.assertEqual(thread.body, "fix the padding")
        self.assertEqual(thread.author, "operator")
        self.assertTrue(thread.id.startswith("operator_note:"))
