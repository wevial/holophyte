"""A route outage backs off across passes without launching dead loops."""
import io
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_fixture import T0, SweepTestCase  # noqa: E402

from holophyte.agents import ProbeResult  # noqa: E402
from holophyte.supervisor import start_loop_for  # noqa: E402


class LaunchBackoffTests(SweepTestCase):
    def fake_commands(self):
        """Real process boundaries: a quota refusal three times, then ready."""
        calls = self.root / "probe.calls"
        starts = self.root / "start.calls"
        probe = self.root / "probe"
        probe.write_text(
            f"#!{sys.executable}\n"
            "from pathlib import Path\n"
            f"calls = Path({str(calls)!r})\n"
            "old = calls.read_text() if calls.exists() else ''\n"
            "calls.write_text(old + 'probe\\n')\n"
            "failed = len(old.splitlines()) < 3\n"
            "print(\"You've hit your usage limit\" if failed else 'ready')\n"
            "raise SystemExit(1 if failed else 0)\n")
        probe.chmod(0o755)
        systemctl = self.root / "systemctl"
        systemctl.write_text(f'#!/bin/sh\necho "$@" >> "{starts}"\n')
        systemctl.chmod(0o755)
        self.configure(f'[agents]\nimplementer = "{probe}"\n')
        return (lambda: calls.read_text().splitlines() if calls.exists() else [],
                lambda: starts.read_text().splitlines() if starts.exists() else [])

    def test_three_failures_then_recovery_and_silent_wait(self):
        probe_calls, starts = self.fake_commands()
        out = io.StringIO()
        with patch.dict(os.environ, {"PATH": f"{self.root}:{os.environ['PATH']}"}):
            now = T0
            for interval in (60, 120, 240):
                start_loop_for(self.tgt, self.conn, [(None, None)], now, out)
                until, reason = self.conn.execute(
                    'SELECT launchBackoffUntil, launchBackoffReason FROM projects'
                ).fetchone()
                self.assertEqual(until - now, interval * 1000)
                self.assertIn("You've hit your usage limit", reason)
                before = out.getvalue()
                start_loop_for(self.tgt, self.conn, [(None, None)], now + 1, out)
                self.assertEqual(out.getvalue(), before)
                now = until
            self.assertEqual(len(out.getvalue().splitlines()), 3)
            self.assertEqual(len(probe_calls()), 3)
            self.assertEqual(starts(), [])
            self.assertEqual(self.conn.execute(
                "SELECT COUNT(*) FROM interventions WHERE action='launch_backoff'"
            ).fetchone()[0], 3)
            notes = self.conn.execute(
                "SELECT summary FROM runEvents WHERE kind='launch_backoff'"
            ).fetchall()
            self.assertEqual(len(notes), 3)
            self.assertTrue(all("You've hit your usage limit" in n[0] for n in notes))
            self.assertEqual(self.conn.execute(
                "SELECT COUNT(*) FROM runEvents WHERE kind='route_down'"
            ).fetchone()[0], 1)
            start_loop_for(self.tgt, self.conn, [(None, None)], now, out)
            self.assertEqual(starts(), ["--user start holophyte-loop@repo"])
            self.assertEqual(self.conn.execute(
                'SELECT launchBackoffUntil, launchBackoffReason FROM projects'
            ).fetchone(), (None, None))
            self.assertEqual(self.conn.execute(
                "SELECT COUNT(*) FROM interventions WHERE action='launch_loop'"
            ).fetchone()[0], 1)

    def test_healthy_first_pass_starts_without_backoff(self):
        with patch('holophyte.agents.probe_implementer',
                   return_value=ProbeResult(['fake-probe'], 0, 'ready', 90)), \
                patch('holophyte.supervisor.start_loop',
                      return_value=('loop', True, '')) as start:
            start_loop_for(self.tgt, self.conn, [(None, None)], T0, io.StringIO())
        start.assert_called_once()
        self.assertEqual(self.conn.execute(
            'SELECT launchBackoffUntil FROM projects').fetchone(), (None,))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM interventions WHERE action='launch_backoff'"
        ).fetchone()[0], 0)

    def test_startup_failure_is_observed_once_and_interval_survives_reopen(self):
        import store
        from store import launch_backoff

        launch_backoff.failure(self.conn, self.project, 'fake-probe: quota',
                               T0, pending=True)
        with patch('holophyte.agents.probe_implementer') as probe, \
                patch('holophyte.supervisor.start_loop') as start:
            start_loop_for(self.tgt, self.conn, [(None, None)], T0 + 1000,
                           io.StringIO())
            probe.assert_not_called()
            start.assert_not_called()
        reopened = store.open(str(self.db))
        self.addCleanup(reopened.close)
        now = T0 + 61_000
        with patch('holophyte.agents.probe_implementer',
                   return_value=ProbeResult(['fake-probe'], 1, 'quota', 90)), \
                patch('holophyte.supervisor.start_loop') as start:
            for interval in (120, 240, 480, 960, 1800, 1800):
                start_loop_for(self.tgt, reopened, [(None, None)], now,
                               io.StringIO())
                state = launch_backoff.current(reopened, self.project)
                self.assertEqual(state['until'], now + interval * 1000)
                self.assertEqual(state['since'], T0)
                now = state['until']
            start.assert_not_called()

    def test_legacy_event_stream_survives_migration_and_accepts_startup_failure(self):
        import store
        from store import launch_backoff

        run = self.a_run()
        self.conn.executescript("""
            ALTER TABLE runEvents RENAME TO savedEvents;
            CREATE TABLE runEvents (
                id INTEGER PRIMARY KEY, runId INTEGER NOT NULL REFERENCES runs(id),
                seq INTEGER NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL,
                summary TEXT NOT NULL, payload TEXT, at INTEGER NOT NULL,
                UNIQUE(runId, seq));
            INSERT INTO runEvents SELECT id, runId, seq, level, kind, summary,
                payload, at FROM savedEvents;
            DROP TABLE savedEvents;
            ALTER TABLE projects DROP COLUMN launchBackoffUntil;
            ALTER TABLE projects DROP COLUMN launchBackoffReason;
            PRAGMA user_version=19;
        """)
        before = self.conn.execute(
            'SELECT id, runId, seq, summary FROM runEvents').fetchall()
        migrated = store.open(str(self.db))
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.execute(
            'SELECT id, runId, seq, summary FROM runEvents').fetchall(), before)
        launch_backoff.failure(migrated, self.project, 'fake-probe: quota', T0)
        self.assertEqual(migrated.execute(
            "SELECT runId, projectId FROM runEvents WHERE kind='route_down'"
        ).fetchall(), [(None, self.project)])
        self.assertEqual(migrated.execute(
            'SELECT id FROM runs').fetchall(), [(run,)])
        self.assertEqual(migrated.execute('PRAGMA foreign_key_check').fetchall(), [])
