"""`factory.py TARGET --approve KO-n [--note TEXT]`: the ticket parked for
merge approval is released, through the command line.

The mode is the store's `approve()` behind argparse, so what is tested here
is the wiring and the contract the ticket states: a parked ticket gets its
`approve` intervention row with the note, its run's resume point at the
merge gate and its status back to `ready`; a ticket in any other state --
ready, in flight, merged -- is refused with a non-zero exit naming that
state and nothing written; a target with no `[board]` table exits naming
the key before the store is touched.

Run: python3 -m unittest discover -s tests -p 'test_cli_*' -v
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


class ApproveCliTests(unittest.TestCase):
    """A target with a store holding one ticket, parked or otherwise."""

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
        self.target.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.target.config_path.write_text(
            '[board]\nproject_id = "p-1"\nteam = "T"\n')
        conn = open_store(self.target)
        self.addCleanup(conn.close)
        self.conn = conn
        self.project = store.ensure_project(conn, "team-1", self.repo)
        self.ticket = store.mirror_ticket(
            conn, self.project, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="a ticket",
            acceptance_criteria=["Given a ticket, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MINUTE)

    def claim(self):
        store.transition(self.conn, self.ticket, "in_flight")
        self.run = store.claim(self.conn, self.project, self.ticket, now=T0)
        return self.run

    def park(self):
        """The state `[merge] approve = "human"` leaves: the run walked to
        the gate and parked there, the ticket blocked asking `merge?`."""
        self.claim()
        for phase in ("working", "verifying", "reviewing", "merge_gate"):
            store.set_phase(self.conn, self.run, phase, now=T0 + MINUTE)
        store.transition(self.conn, self.ticket, "blocked_on_operator")
        self.conn.execute(
            "UPDATE tickets SET blockedQuestion = 'merge?' WHERE id = ?",
            (self.ticket,))
        self.conn.commit()
        store.park(self.conn, self.run, "awaiting_merge_approval",
                   now=T0 + 2 * MINUTE)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            holophyte.cli.cli([str(self.repo), *args])
        return out.getvalue(), err.getvalue()

    def interventions(self):
        return self.conn.execute(
            'SELECT runId, "action" FROM interventions').fetchall()

    def ticket_row(self):
        return self.conn.execute(
            "SELECT status, activeRunId, lastRunId FROM tickets WHERE id = ?",
            (self.ticket,)).fetchone()

    def run_row(self):
        return self.conn.execute(
            "SELECT phase, outcome, resumePhase, endedAt FROM runs"
            " WHERE id = ?", (self.run,)).fetchone()

    def test_approve_releases_the_parked_run_and_readies_the_ticket(self):
        self.park()

        out, _ = self.cli("--approve", "KO-1", "--note", "ok")

        self.assertIn(f"KO-1 approved: run {self.run}", out)
        self.assertEqual(self.interventions(), [(self.run, "approve")])
        (summary,) = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind ="
            " 'intervention'", (self.run,)).fetchone()
        self.assertEqual(summary, "human approve: ok")
        phase, outcome, resume_phase, ended = self.run_row()
        self.assertEqual(resume_phase, "merge_gate")
        self.assertNotEqual(phase, "awaiting_merge_approval")
        self.assertIsNotNone(ended)
        self.assertEqual(self.ticket_row(), ("ready", None, self.run))
        # Released, the ticket is claimable again -- the loop's next pass
        # is what takes the candidate to the gate.
        self.assertTrue(store.pickable(self.conn, self.ticket))

    def test_a_bare_approve_records_the_default_note(self):
        self.park()

        self.cli("--approve", "KO-1")

        (summary,) = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind ="
            " 'intervention'", (self.run,)).fetchone()
        self.assertEqual(
            summary, f"human approve: {holophyte.cli.APPROVE_DEFAULT_NOTE}")

    def test_a_ticket_not_parked_is_refused_naming_its_state(self):
        """Ready with no run, in flight with a live run, merged: each exits
        non-zero naming the ticket's state, and the store is untouched."""
        with self.assertRaises(SystemExit) as ready:
            self.cli("--approve", "KO-1", "--note", "ok")
        self.assertIn("KO-1 is ready", str(ready.exception))

        self.claim()
        with self.assertRaises(SystemExit) as live:
            self.cli("--approve", "KO-1", "--note", "ok")
        self.assertIn("KO-1 is in_flight", str(live.exception))
        self.assertIn(f"run {self.run} still live", str(live.exception))
        self.assertEqual(self.run_row()[:2], ("claimed", None))

        store.release(self.conn, self.run, "merged", now=T0 + MINUTE)
        store.transition(self.conn, self.ticket, "merged")
        with self.assertRaises(SystemExit) as merged:
            self.cli("--approve", "KO-1", "--note", "ok")
        self.assertIn("KO-1 is merged, not blocked_on_operator",
                      str(merged.exception))
        self.assertEqual(self.ticket_row(), ("merged", None, self.run))

        with self.assertRaises(SystemExit) as unknown:
            self.cli("--approve", "KO-404")
        self.assertIn("KO-404", str(unknown.exception))

        for raised in (ready, live, merged, unknown):
            self.assertNotEqual(raised.exception.code, 0)
        self.assertEqual(self.interventions(), [])
        self.assertEqual(self.run_row()[2], None)

    def test_a_ticket_walked_off_the_park_by_hand_is_refused_by_status(self):
        """The run still sits in `awaiting_merge_approval`, but an operator
        walked the ticket on to `ready` from the REPL: the status decides,
        the refusal names it, and the parked run is left as it was."""
        self.park()
        store.walk_ticket(self.conn, self.ticket, "ready")
        self.assertEqual(self.run_row()[0], "awaiting_merge_approval")

        with self.assertRaises(SystemExit) as raised:
            self.cli("--approve", "KO-1", "--note", "ok")

        self.assertNotEqual(raised.exception.code, 0)
        self.assertIn("KO-1 is ready, not blocked_on_operator",
                      str(raised.exception))
        self.assertEqual(self.interventions(), [])
        self.assertEqual(self.run_row(),
                         ("awaiting_merge_approval", None, None, None))
        self.assertEqual(self.ticket_row(), ("ready", None, self.run))

    def test_a_target_with_no_board_exits_naming_the_key_and_writes_nothing(self):
        self.park()
        self.target.config_path.unlink()

        with patch.dict(os.environ, {"HOLO2_PROJECT_ID": "p-env",
                                     "HOLO2_TEAM": "T"}), \
                self.assertRaises(SystemExit) as raised:
            self.cli("--approve", "KO-1")

        self.assertNotEqual(raised.exception.code, 0)
        self.assertIn("[board] project_id", str(raised.exception))
        self.assertEqual(self.ticket_row()[0], "blocked_on_operator")
        self.assertEqual(self.interventions(), [])

    def test_a_blank_note_with_approve_is_an_argparse_error(self):
        self.park()
        with self.assertRaises(SystemExit) as raised:
            self.cli("--approve", "KO-1", "--note", "  ")
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.interventions(), [])


if __name__ == "__main__":
    unittest.main()
