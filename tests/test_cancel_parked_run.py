"""A native-board `--cancel` whose ticket's last run is parked ends that run
`abandoned` behind a `close_out` intervention and leaves its pull request
open; a run that already ended is left alone, and a run parked after its
merge landed refuses the cancel. The command line runs against
a real store under a throwaway home, and Linear's transport refuses to be
called.

Run: python3 -m unittest discover -s tests -p 'test_cancel_parked_run.py' -v
"""
import contextlib
import io
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert

import holophyte.cli.entry  # noqa: E402
import linear_provider  # noqa: E402
import store  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from tests.test_cli_native_update import NATIVE, body, no_linear  # noqa: E402

PULL = "https://github.com/o/r/pull/7"


class CancelParkedRunTests(ConfigTestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.locate(NATIVE)
        self.gh_calls = self.root / "gh-calls"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(f'#!/bin/sh\necho "$@" >> {self.gh_calls}\n')
        gh.chmod(0o755)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        (self.target / "tests").mkdir()
        (self.target / "tests" / "test_thing.py").write_text("")
        path = self.root / "T.md"
        path.write_text(body("Thing"))
        self.assertEqual(self.cli("--file-ticket", str(path))[0], 0)

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = holophyte.cli.entry.cli([str(self.target), *args])
        return status, out.getvalue()

    def parked_run(self, pr_url=None):
        """NAT-1's run parked awaiting merge approval, its lease given back."""
        with contextlib.closing(open_store(self.project)) as conn:
            project_id, ticket_id = conn.execute(
                "SELECT projectId, id FROM tickets").fetchone()
            run = store.claim(conn, project_id, ticket_id)
            store.tickets.transition(conn, ticket_id, "in_flight")
            store.set_phase(conn, run, "working")
            store.set_phase(conn, run, "verifying")
            store.park(conn, run, "awaiting_merge_approval", pr_url=pr_url)
            store.tickets.transition(conn, ticket_id, "blocked_on_operator")
        return run

    def cancel(self):
        status, printed = self.cli("--cancel", "NAT-1", "--revision", "1",
                                   "--note", "wrong scope")
        self.assertEqual(status, 0, printed)
        return printed

    def read(self, sql):
        with contextlib.closing(sqlite3.connect(self.project.store_path)) as conn:
            return conn.execute(sql).fetchall()

    def test_a_cancel_ends_a_run_parked_awaiting_approval_after_a_close_out(self):
        run = self.parked_run()
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])

        printed = self.cancel()

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("abandoned",)])
        self.assertEqual(self.read("SELECT status FROM tickets"), [("abandoned",)])
        self.assertEqual(self.read(
            'SELECT action, source, "trigger" FROM interventions'
            " WHERE runId IS NOT NULL"),
            [("close_out", "human", "board_cancelled")])
        self.assertEqual(self.read(
            "SELECT kind, summary FROM runEvents"
            " WHERE kind IN ('intervention', 'phase_change') ORDER BY seq")[-2:],
            [("intervention", "human close_out: NAT-1 canceled on the board;"
              f" run {run} ended abandoned"),
             ("phase_change", "awaiting_merge_approval -> failed:"
              " run ended, outcome abandoned")])
        self.assertIn(f"run {run} ended abandoned", printed)

    def test_a_cancel_ends_a_run_parked_on_a_pull_request_and_leaves_it_open(self):
        self.parked_run(pr_url=PULL)

        printed = self.cancel()

        self.assertEqual(self.read("SELECT outcome, prUrl FROM runs"),
                         [("abandoned", PULL)])
        self.assertEqual(self.read("SELECT status FROM tickets"), [("abandoned",)])
        self.assertEqual(self.read("SELECT action FROM interventions"
                                   " WHERE runId IS NOT NULL"),
                         [("close_out",)])
        self.assertFalse(self.gh_calls.exists(), "the cancel ran gh")
        self.assertIn(f"{PULL} left open", printed)

    def test_status_and_report_no_longer_list_the_canceled_run_in_flight(self):
        run = self.parked_run(pr_url=PULL)
        self.cancel()

        status, printed = self.cli("--status")
        self.assertEqual(status, 0, printed)
        self.assertNotIn(f"run {run}", printed)
        printed = self.cli("--report")[1]
        self.assertIn("in flight: none", printed)

    def test_a_cancel_leaves_a_run_that_already_ended_untouched(self):
        run = self.parked_run()
        with contextlib.closing(open_store(self.project)) as conn:
            store.release(conn, run, "failed", "verify failed")
        self.assert_cancel_leaves(run)

    def test_a_cancel_refuses_a_ticket_whose_run_parked_after_its_merge(self):
        with contextlib.closing(open_store(self.project)) as conn:
            project_id, ticket_id = conn.execute(
                "SELECT projectId, id FROM tickets").fetchone()
            run = store.claim(conn, project_id, ticket_id)
            store.tickets.transition(conn, ticket_id, "in_flight")
            for phase in ("working", "verifying", "reviewing", "merge_gate",
                          "merging"):
                store.set_phase(conn, run, phase)
            store.park(conn, run, "blocked_on_operator", "after command failed")
            store.tickets.transition(conn, ticket_id, "blocked_on_operator")
        runs = "SELECT phase, outcome, endedAt FROM runs"
        tickets = "SELECT status, boardColumn, revision FROM tickets"
        before = self.read(runs), self.read(tickets)

        status, printed = self.cli("--cancel", "NAT-1", "--revision", "1",
                                   "--note", "wrong scope")

        self.assertEqual(status, 1, printed)
        self.assertIn(f"run {run} is parked blocked_on_operator", printed)
        self.assertEqual((self.read(runs), self.read(tickets)), before)
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"
                                   " WHERE runId IS NOT NULL"), [(0,)])

    def assert_cancel_leaves(self, run):
        before = self.read("SELECT phase, outcome, endedAt FROM runs")

        printed = self.cancel()

        self.assertNotIn(f"run {run}", printed)
        self.assertEqual(self.read("SELECT phase, outcome, endedAt FROM runs"),
                         before)
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"
                                   " WHERE runId IS NOT NULL"), [(0,)])
