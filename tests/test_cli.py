"""`factory.py TARGET --repoint KO-n SHA --note TEXT`: a parked candidate
moved to a rebuilt branch tip, through the command line (KO-297).

The mode is the store's `repoint()` behind argparse, so what is tested here
is the wiring: `--note` is required the way `--requeue` requires it and the
refusal never reaches the store, the two shas are printed on success, and a
store refusal is a non-zero exit naming the ticket with nothing written.

Run: python3 -m unittest discover -s tests -p 'test_cli.py' -v
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli
import holophyte.target
import store
from holophyte.runs import open_store

MINUTE = 60 * 1000
T0 = 1_700_000_000_000
OLD_SHA = "0bbd7e6100000000000000000000000000000000"
NEW_SHA = "7af190c000000000000000000000000000000000"


class RepointFlagTests(unittest.TestCase):
    """A target with a store holding one ticket and its parked run."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = patch.dict(os.environ,
                             {"HOLOPHYTE_HOME": str(self.root / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.target = holophyte.target.Target.locate(self.repo)
        conn = open_store(self.target)
        self.addCleanup(conn.close)
        self.conn = conn
        self.project = store.ensure_project(conn, "team-1", self.repo)
        self.ticket = store.mirror_ticket(
            conn, self.project, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="a ticket",
            acceptance_criteria=["Given a ticket, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MINUTE)
        store.transition(conn, self.ticket, "in_flight")
        self.run = store.claim(conn, self.project, self.ticket, now=T0)
        for phase in ("working", "verifying", "reviewing", "merge_gate"):
            store.set_phase(conn, self.run, phase, now=T0 + MINUTE)

    def park(self):
        store.transition(self.conn, self.ticket, "blocked_on_operator")
        store.park(self.conn, self.run, "awaiting_merge_approval",
                   candidate_sha=OLD_SHA, now=T0 + 2 * MINUTE)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            holophyte.cli.cli([str(self.repo), *args])
        return out.getvalue(), err.getvalue()

    def candidate_sha(self):
        return self.conn.execute(
            "SELECT candidateSha FROM runs WHERE id = ?",
            (self.run,)).fetchone()[0]

    def test_note_is_required(self):
        self.park()
        with patch.object(store, "repoint") as repoint:
            with self.assertRaises(SystemExit) as raised:
                self.cli("--repoint", "KO-1", NEW_SHA)
        # argparse's usage exit, the one `--requeue` without a note takes,
        # before any store is opened.
        self.assertEqual(raised.exception.code, 2)
        repoint.assert_not_called()
        self.assertEqual(self.candidate_sha(), OLD_SHA)

    def test_repoint_moves_the_sha_and_prints_both(self):
        self.park()

        out, _ = self.cli("--repoint", "KO-1", NEW_SHA,
                          "--note", "rebased onto the filtered main")

        self.assertIn(OLD_SHA, out)
        self.assertIn(NEW_SHA, out)
        self.assertEqual(self.candidate_sha(), NEW_SHA)
        self.assertEqual(
            self.conn.execute('SELECT runId, "action", guidance'
                              " FROM interventions").fetchall(),
            [(self.run, "repoint", "rebased onto the filtered main")])

    def test_a_refusal_exits_non_zero_naming_the_ticket_and_writes_nothing(self):
        # Not parked: the run is live at the gate.
        with self.assertRaises(SystemExit) as raised:
            self.cli("--repoint", "KO-1", NEW_SHA, "--note", "rebased")
        self.assertIn("KO-1", str(raised.exception))
        self.assertIsNone(self.candidate_sha())
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM interventions")
            .fetchone(), (0,))


if __name__ == "__main__":
    unittest.main()
