"""The read-only report shows unfinished work before its finished history."""
import io
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import holophyte.cli
import holophyte.report as report
import store
import store.tickets
from tests.phase_fixture import advance_phase, finish_run
from tests.test_wiring_telemetry import ReportStoreCase

NOW = 1_700_010_000_000
URL = "https://github.com/example/repo/pull/454"
# Captured from report_lines() before the live block was added.
FINISHED = (
    "ticket  actual  agent  verify  estimate  ratio  rounds  outcome  rejected"
    "  host\n"
    "KO-1       5.0    5.0     0.0        25   0.20       0  merged          0"
    "  writer\n"
    "1 runs · mean ratio 0.20 · median ratio 0.20"
)


class LiveReportTests(ReportStoreCase):
    def setUp(self):
        super().setUp()
        with patch("socket.gethostname", return_value="writer"):
            self.completed_run(1, 5, 25, 0, "merged")

    def test_failure_counts_in_report_and_sweep(self):
        from holophyte.project import Project
        from holophyte.sweep_report import sweep_report

        for number, kind in enumerate(('verify', 'infra', 'verify', 'budget'), 20):
            run = self.live_run(number, NOW - 1000, 'working')
            store.release(self.conn, run, 'failed', 'unchanged reason',
                          failure_kind=kind)
        expected = ['failures budget: 1', 'failures infra: 1', 'failures verify: 2']
        self.assertEqual([line for line in report.report_lines(self.conn)
                          if line.startswith('failures ')], expected)
        out = io.StringIO()
        with patch('holophyte.sweep_report.review_container_lines', return_value=[]):
            sweep_report(Project.locate(self.target), conn=self.conn, out=out, now=NOW)
        for line in expected:
            self.assertIn(line, out.getvalue().splitlines())

    def test_operator_commands_allow_bucket_without_credentials(self):
        (self.db.parent / "config.toml").write_text(
            '[merge.media_bucket]\nendpoint = "https://objects.example.invalid"\n'
            'bucket = "evidence"\npublic_base = "https://media.example.invalid"\n')
        env = dict(os.environ)
        for name in ("HOLOPHYTE_MEDIA_ACCESS_KEY_ID",
                     "HOLOPHYTE_MEDIA_SECRET_ACCESS_KEY"):
            env.pop(name, None)
        for command in ("--report", "--sweep"):
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, "factory.py", str(self.target), command],
                    cwd=Path(__file__).resolve().parents[1], env=env,
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("warning: media_bucket", result.stdout)

    def live_run(self, number, started, phase, url=None, project=None):
        project = self.project if project is None else project
        ticket = store.tickets.mirror_ticket(
            self.conn, project, linear_issue_id=f"issue-{number}",
            linear_identifier=f"KO-{number}", title="live ticket")
        run = store.claim(self.conn, project, ticket, now=started)
        advance_phase(self.conn, run, phase, now=NOW - 12_000)
        self.conn.execute("UPDATE runs SET prUrl = ? WHERE id = ?", (url, run))
        self.conn.commit()
        return run

    def test_concurrent_completion_appears_only_in_live_snapshot(self):
        run = self.live_run(454, NOW - 19 * 60_000, "merge_gate", URL)
        writer = store.open(str(self.db), migrate="owner")
        self.addCleanup(writer.close)
        read_finished = report.report_rows

        def finish_between_reads(conn):
            finish_run(writer, run, "merged", now=NOW)
            return read_finished(conn)

        with patch.object(report, "report_rows", side_effect=finish_between_reads):
            lines = report.report_lines(self.conn)
        self.assertEqual(lines[0], "in flight:")
        self.assertTrue(lines[1].startswith("KO-454  merge_gate"))
        self.assertEqual("\n".join(lines[3:]), FINISHED)
        self.assertFalse(self.conn.in_transaction)
        following = report.report_lines(self.conn)
        self.assertEqual(following[0], "in flight: none")
        self.assertTrue(any(line.startswith("KO-454 ") for line in following[2:]))

    def test_caller_transaction_survives_success_and_read_failure(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                self.conn.execute("UPDATE runs SET host = 'pending'")
                if fails:
                    with patch.object(report, "report_rows",
                                      side_effect=RuntimeError("read failed")):
                        with self.assertRaisesRegex(RuntimeError, "read failed"):
                            report.report_lines(self.conn)
                else:
                    self.assertIn("pending", "\n".join(report.report_lines(self.conn)))
                self.assertTrue(self.conn.in_transaction)
                self.assertEqual(
                    self.conn.execute("SELECT host FROM runs").fetchone()[0],
                    "pending")
                self.conn.rollback()
                self.assertEqual(
                    self.conn.execute("SELECT host FROM runs").fetchone()[0],
                    "writer")

    def test_failed_reads_leave_no_report_transaction(self):
        for reader in ("live_lines", "report_rows"):
            with self.subTest(reader=reader):
                with patch.object(report, reader,
                                  side_effect=RuntimeError("read failed")):
                    with self.assertRaisesRegex(RuntimeError, "read failed"):
                        report.report_lines(self.conn)
                self.assertFalse(self.conn.in_transaction)
                self.assertEqual(report.report_lines(self.conn)[0], "in flight: none")

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
        self.assertEqual(out.getvalue().splitlines()[1:4], expected)

    def test_no_unfinished_runs(self):
        self.assertEqual(report.report_lines(self.conn),
                         ["in flight: none", "", *FINISHED.splitlines()])

    def test_actual_is_split_into_agent_and_verify_columns(self):
        self.completed_run(2, 5, 10, 0, "merged")
        self.completed_run(3, 4, 10, 0, "merged")
        for ident, verify_ms in (("KO-2", 3 * 60_000), ("KO-3", None)):
            # KO-3 was recorded before the store split out verify time.
            self.conn.execute(
                "UPDATE runs SET verifyMs = ? WHERE ticketId ="
                " (SELECT id FROM tickets WHERE linearIdentifier = ?)",
                (verify_ms, ident))
        self.conn.commit()
        lines = report.report_lines(self.conn)
        header = next(line for line in lines if line.startswith("ticket "))
        self.assertEqual(header.split()[1:6],
                         ["actual", "agent", "verify", "estimate", "ratio"])
        cells = {line.split()[0]: line.split()[1:6] for line in lines
                 if line.startswith(("KO-2 ", "KO-3 "))}
        self.assertEqual(cells, {"KO-2": ["5.0", "2.0", "3.0", "10", "0.50"],
                                 "KO-3": ["4.0", "4.0", "n/a", "10", "0.40"]})

    def test_live_rows_are_chronological_and_missing_url_has_no_padding(self):
        self.live_run(454, NOW - 9 * 60_000, "working")
        project = store.tickets.ensure_project(self.conn, "team-2", "/repos/other")
        self.live_run(455, NOW - 19 * 60_000, "merge_gate", URL, project)
        self.assertEqual(report.live_lines(self.conn, NOW), [
            "in flight:",
            f"KO-455  merge_gate  19m  hb 12s  {URL}",
            "KO-454  working      9m  hb 12s",
        ])


class MigrationReportTests(ReportStoreCase):
    def test_cli_header_describes_latest_migration(self):
        import json
        self.conn.execute(
            "UPDATE interventions SET note = ? WHERE action = 'migrate'",
            (json.dumps({"from": 21, "to": store.SCHEMA_VERSION,
                         "build": "abc1234", "pid": 4321,
                         "argv": ["factory.py", "--report"], "at": NOW}),))
        self.conn.commit()
        self.completed_run(1, 5, 25, 0, "merged")
        run = self.conn.execute("SELECT id FROM runs").fetchone()[0]
        store.record_intervention(self.conn, run, "migrate", "operator note")
        out = io.StringIO()
        with patch.object(sys, "stdout", out):
            holophyte.cli.cli(["--report", str(self.target)])
        line = next(line for line in out.getvalue().splitlines()
                    if line.startswith("store schema"))
        self.assertIn(
            f"store schema {store.SCHEMA_VERSION} (migrated from 21 at ", line)
        self.assertIn("by abc1234, pid 4321, factory.py)", line)

    def test_readers_allow_a_store_without_migration_history(self):
        import holophyte.serve_runs
        self.conn.execute("DELETE FROM interventions WHERE action = 'migrate'")
        self.conn.execute("ALTER TABLE interventions DROP COLUMN note")
        self.conn.commit()
        self.assertEqual(report.migration_header(self.conn), [])
        self.assertEqual(
            holophyte.serve_runs.migration_rows(self.conn, 0, 10, self.project), [])
