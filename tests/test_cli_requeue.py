"""`factory.py TARGET --requeue KO-n --note TEXT`: the failed ticket goes back
in the queue with its intervention row, through the command line.

The mode is the store's `requeue()` behind argparse, so what is tested here
is the wiring: the identifier resolves in the target's store, the requeued
line is printed, a refusal is a non-zero exit naming the reason with nothing
written, and a `--requeue` with no `--note` never reaches the store at all.

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


class RequeueCliTests(unittest.TestCase):
    """A target with a store holding one ticket and its ended (or live) run."""

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

    def fail_the_run(self):
        store.release(self.conn, self.run, "failed", "verify failed",
                      now=T0 + MINUTE)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            holophyte.cli.cli([str(self.repo), *args])
        return out.getvalue(), err.getvalue()

    def interventions(self):
        return self.conn.execute(
            'SELECT runId, "action" FROM interventions').fetchall()

    def status(self):
        return self.conn.execute(
            "SELECT status FROM tickets WHERE id = ?",
            (self.ticket,)).fetchone()[0]

    def test_requeue_walks_the_failed_ticket_to_ready_with_its_row(self):
        self.fail_the_run()

        out, _ = self.cli("--requeue", "KO-1", "--note", "contract fixed")

        self.assertEqual(out.strip(), f"[holo2] KO-1 requeued after run {self.run}")
        self.assertEqual(self.status(), "ready")
        self.assertEqual(self.interventions(), [(self.run, "requeue")])
        (summary,) = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind ="
            " 'intervention'", (self.run,)).fetchone()
        self.assertIn("contract fixed", summary)

    def test_each_refusal_exits_non_zero_naming_it_and_writes_nothing(self):
        # A live run first, then the same ticket once it is ready, then an
        # identifier the store never mirrored: three refusals, no row.
        with self.assertRaises(SystemExit) as live:
            self.cli("--requeue", "KO-1", "--note", "too soon")
        self.assertIn("still live", str(live.exception))
        self.assertEqual(self.status(), "in_flight")

        self.fail_the_run()
        self.cli("--requeue", "KO-1", "--note", "contract fixed")
        before = self.interventions()
        with self.assertRaises(SystemExit) as ready:
            self.cli("--requeue", "KO-1", "--note", "again")
        self.assertIn("is ready", str(ready.exception))

        with self.assertRaises(SystemExit) as unknown:
            self.cli("--requeue", "KO-404", "--note", "who?")
        self.assertIn("KO-404", str(unknown.exception))

        for raised in (live, ready, unknown):
            self.assertNotEqual(raised.exception.code, 0)
        self.assertEqual(self.interventions(), before)

    def test_requeue_without_a_note_is_an_argparse_error(self):
        self.fail_the_run()
        with patch.object(store, "requeue") as requeue:
            with self.assertRaises(SystemExit) as raised:
                _, err = self.cli("--requeue", "KO-1")
        # argparse's usage exit, before any store is opened.
        self.assertEqual(raised.exception.code, 2)
        requeue.assert_not_called()
        self.assertEqual(self.status(), "in_flight")

    def test_a_note_without_requeue_is_an_argparse_error(self):
        with self.assertRaises(SystemExit) as raised:
            self.cli("--report", "--note", "stray")
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
