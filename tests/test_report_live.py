"""The read-only report shows unfinished work before its finished history."""
import io
import sys
from unittest.mock import patch

import holophyte.cli
import holophyte.report as report
import store
import store.tickets
from tests.test_wiring_telemetry import ReportStoreCase

NOW = 1_700_010_000_000
URL = "https://github.com/example/repo/pull/454"
# Captured from report_lines() before the live block was added.
FINISHED = (
    "ticket  actual  estimate  ratio  rounds  outcome  rejected  host\n"
    "KO-1       5.0        25   0.20       0  merged          0  writer\n"
    "1 runs · mean ratio 0.20 · median ratio 0.20"
)


class LiveReportTests(ReportStoreCase):
    def setUp(self):
        super().setUp()
        with patch("socket.gethostname", return_value="writer"):
            self.completed_run(1, 5, 25, 0, "merged")

    def live_run(self, number, started, phase, url=None, project=None):
        project = self.project if project is None else project
        ticket = store.tickets.mirror_ticket(
            self.conn, project, linear_issue_id=f"issue-{number}",
            linear_identifier=f"KO-{number}", title="live ticket")
        run = store.claim(self.conn, project, ticket, now=started)
        store.set_phase(self.conn, run, phase, now=NOW - 12_000)
        self.conn.execute("UPDATE runs SET prUrl = ? WHERE id = ?", (url, run))
        self.conn.commit()

    def test_live_block_precedes_unchanged_finished_table_and_cli(self):
        self.live_run(454, NOW - 19 * 60_000, "merge_gate", URL)
        expected = ["in flight:", f"KO-454  merge_gate  19m  hb 12s  {URL}", ""]
        with patch("holophyte.report.time.time", return_value=NOW / 1000):
            lines = report.report_lines(self.conn)
            self.assertEqual(lines[:3], expected)
            self.assertEqual("\n".join(lines[3:]), FINISHED)
            out = io.StringIO()
            with patch.object(sys, "stdout", out):
                holophyte.cli.cli(["--report", str(self.target)])
        self.assertEqual(out.getvalue().splitlines()[:3], expected)

    def test_no_unfinished_runs(self):
        self.assertEqual(report.report_lines(self.conn),
                         ["in flight: none", "", *FINISHED.splitlines()])

    def test_live_rows_are_chronological_and_missing_url_has_no_padding(self):
        self.live_run(454, NOW - 9 * 60_000, "working")
        project = store.tickets.ensure_project(self.conn, "team-2", "/repos/other")
        self.live_run(455, NOW - 19 * 60_000, "merge_gate", URL, project)
        self.assertEqual(report.live_lines(self.conn, NOW), [
            "in flight:",
            f"KO-455  merge_gate  19m  hb 12s  {URL}",
            "KO-454  working      9m  hb 12s",
        ])
