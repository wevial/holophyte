"""A route outage backs off across passes without launching dead loops."""
import contextlib
import io
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loop_fixture import VALID_BODY  # noqa: E402
from sweep_fixture import T0, SweepTestCase  # noqa: E402

from holophyte.agents.probes import ProbeResult  # noqa: E402
from holophyte.host.supervisor import start_loop_for  # noqa: E402
from provider import FileProvider  # noqa: E402


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
                start_loop_for(self.project, self.conn, [(None, None)], now, out)
                until, reason = self.conn.execute(
                    'SELECT launchBackoffUntil, launchBackoffReason FROM projects'
                ).fetchone()
                self.assertEqual(until - now, interval * 1000)
                self.assertIn("You've hit your usage limit", reason)
                before = out.getvalue()
                start_loop_for(self.project, self.conn, [(None, None)], now + 1, out)
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
            start_loop_for(self.project, self.conn, [(None, None)], now, out)
            self.assertEqual(starts(), ["--user start holophyte-loop@repo"])
            self.assertEqual(self.conn.execute(
                'SELECT launchBackoffUntil, launchBackoffReason FROM projects'
            ).fetchone(), (None, None))
            self.assertEqual(self.conn.execute(
                "SELECT COUNT(*) FROM interventions WHERE action='launch_loop'"
            ).fetchone()[0], 1)

    def test_healthy_first_pass_starts_without_backoff(self):
        with patch('holophyte.agents.probes.probe_implementer',
                   return_value=ProbeResult(['fake-probe'], 0, 'ready', 90)), \
                patch('holophyte.host.supervisor.start_loop',
                      return_value=('loop', True, '')) as start:
            start_loop_for(self.project, self.conn, [(None, None)], T0, io.StringIO())
        start.assert_called_once()
        self.assertEqual(self.conn.execute(
            'SELECT launchBackoffUntil FROM projects').fetchone(), (None,))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM interventions WHERE action='launch_backoff'"
        ).fetchone()[0], 0)

    def test_startup_failure_is_observed_once_and_interval_survives_reopen(self):
        import store
        from store import launch_backoff

        launch_backoff.failure(self.conn, self.project_id, 'fake-probe: quota',
                               T0, pending=True)
        with patch('holophyte.agents.probes.probe_implementer') as probe, \
                patch('holophyte.host.supervisor.start_loop') as start:
            start_loop_for(self.project, self.conn, [(None, None)], T0 + 1000,
                           io.StringIO())
            probe.assert_not_called()
            start.assert_not_called()
        reopened = store.open(str(self.db))
        self.addCleanup(reopened.close)
        now = T0 + 61_000
        with patch('holophyte.agents.probes.probe_implementer',
                   return_value=ProbeResult(['fake-probe'], 1, 'quota', 90)), \
                patch('holophyte.host.supervisor.start_loop') as start:
            for interval in (120, 240, 480, 960, 1800, 1800):
                start_loop_for(self.project, reopened, [(None, None)], now,
                               io.StringIO())
                state = launch_backoff.current(reopened, self.project_id)
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
        launch_backoff.failure(migrated, self.project_id, 'fake-probe: quota', T0)
        self.assertEqual(migrated.execute(
            "SELECT runId, projectId FROM runEvents WHERE kind='route_down'"
        ).fetchall(), [(None, self.project_id)])
        self.assertEqual(migrated.execute(
            'SELECT id FROM runs').fetchall(), [(run,)])
        self.assertEqual(migrated.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_successful_manual_startup_clears_the_outage_without_owed_work(self):
        from types import SimpleNamespace

        from holophyte.cli import operator
        from holophyte.serve.serve_runs import route_down_rows
        from store import launch_backoff

        launch_backoff.failure(self.conn, self.project_id, 'quota exhausted', T0)
        with contextlib.redirect_stdout(io.StringIO()), \
                patch('holophyte.cli.operator.probe_implementer',
                   return_value=ProbeResult(['fake-probe'], 0, 'ready', 90)), \
                patch('holophyte.cli.operator._serial', return_value=0):
            self.assertEqual(operator.main(
                self.project, SimpleNamespace(team='team-1')), 0)
        self.assertIsNone(launch_backoff.current(self.conn, self.project_id))
        self.assertEqual(route_down_rows(self.conn), [])

    def test_unclaimed_ticket_and_board_fallback_use_their_own_project(self):
        from types import SimpleNamespace

        import store
        from holophyte.host.supervisor import reconcile_parked_pull_requests
        from store import launch_backoff

        project_id = store.ensure_project(self.conn, 'team-2', self.target)
        ticket = store.mirror_ticket(
            self.conn, project_id, linear_issue_id='unclaimed',
            linear_identifier='KO-2', title='unclaimed',
            acceptance_criteria=['Given work, then it is done'],
            verification_commands=['echo ok'])
        provider = SimpleNamespace(team='team-2')
        failure = ProbeResult(['fake-probe'], 1, 'quota exhausted', 90)
        with (
            patch('holophyte.agents.probes.probe_implementer', return_value=failure),
            patch('holophyte.host.supervisor.start_loop') as start,
            patch('holophyte.host.reconcile._reconcile_pull_requests'),
            patch('holophyte.host.supervisor.linear_budget_low', return_value=False),
            patch('holophyte.host.supervisor.board_ready', return_value=1) as board,
        ):
            reconcile_parked_pull_requests(
                self.project, self.conn, T0, provider, io.StringIO())
            board.assert_not_called()
            self.assertIsNone(launch_backoff.current(self.conn, self.project_id))
            self.assertIsNotNone(launch_backoff.current(self.conn, project_id))
            launch_backoff.clear(self.conn, project_id)
            store.transition(self.conn, ticket, 'blocked_on_deps')
            reconcile_parked_pull_requests(
                self.project, self.conn, T0 + 1000, provider, io.StringIO())
            board.assert_called_once()
            start.assert_not_called()
            self.assertIsNone(launch_backoff.current(self.conn, self.project_id))
            self.assertIsNotNone(launch_backoff.current(self.conn, project_id))

    def test_probe_credentials_are_redacted_in_output_and_store(self):
        from types import SimpleNamespace

        from holophyte.cli import operator
        from store import launch_backoff

        secret = 'fixture-credential-12345'
        self.configure(f'[service]\napi_key = "{secret}"\n')
        for startup in (True, False):
            for code, output in ((1, f'quota exhausted: {secret}'), (0, 'ready')):
                with self.subTest(startup=startup, code=code):
                    launch_backoff.clear(self.conn, self.project_id)
                    probe = ProbeResult(['fake-probe', secret], code, output, 90)
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out), \
                            patch('holophyte.cli.operator.probe_implementer',
                                  return_value=probe), \
                            patch('holophyte.agents.probes.probe_implementer',
                                  return_value=probe), \
                            patch('holophyte.cli.operator._serial', return_value=0), \
                            patch('holophyte.host.supervisor.start_loop',
                                  return_value=('loop', True, '')):
                        if startup:
                            operator.main(self.project, SimpleNamespace(team='team-1'))
                        else:
                            start_loop_for(self.project, self.conn, [(None, None)], T0,
                                           out)
                    evidence = out.getvalue() + str(self.conn.execute(
                        'SELECT summary FROM runEvents').fetchall()) + str(
                        launch_backoff.current(self.conn, self.project_id))
                    self.assertNotIn(secret, evidence)
                    if code:
                        self.assertIn('quota exhausted', evidence)
                        self.assertIn('[redacted]', evidence)


class StoreModeFiles(FileProvider):
    store_mode = True


class LoopEnvironmentProbeTests(SweepTestCase):
    def fixture(self, probe_lines, serve_env, agents=""):
        """A released store-mode project whose implementer is `probe_lines`."""
        from holophyte.host.registry import home

        self.calls, self.starts = self.root / "probe.calls", self.root / "start.calls"
        probe = self.root / "probe"
        probe.write_text(
            f"#!{sys.executable}\n"
            "import os\nfrom pathlib import Path\n"
            f"calls = Path({str(self.calls)!r})\n"
            "calls.write_text((calls.read_text() if calls.exists() else '')"
            " + 'probe\\n')\n" + probe_lines)
        probe.chmod(0o755)
        systemctl = self.root / "systemctl"
        systemctl.write_text(f'#!/bin/sh\necho "$@" >> "{self.starts}"\n')
        systemctl.chmod(0o755)
        self.configure(
            '[board]\nproject_id = "project-1"\nteam = "team-1"\n'
            f'mode = "store"\n[agents]\nimplementer = "{probe}"\n{agents}')
        unit_dir = home() / "repo"
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / "serve.env").write_text(serve_env)
        files = self.root / "team-1"
        files.mkdir()
        (files / "KO-1.md").write_text(VALID_BODY)
        self.board = StoreModeFiles(files)

    def one_pass(self, at, out=None):
        from holophyte.host.supervisor import reconcile_parked_pull_requests

        reconcile_parked_pull_requests(
            self.project, self.conn, at, self.board, out or io.StringIO())

    def count(self, action):
        return self.conn.execute(
            "SELECT COUNT(*) FROM interventions WHERE action = ?",
            (action,)).fetchone()[0]

    def started(self):
        return self.starts.read_text().splitlines() if self.starts.exists() else []

    @contextlib.contextmanager
    def sweep_environment(self):
        environ = {k: v for k, v in os.environ.items()
                   if k != "HOLO_FIXTURE_CREDENTIAL"}
        environ["PATH"] = f"{self.root}:{os.environ['PATH']}"
        with (
            patch.dict(os.environ, environ, clear=True),
            patch('holophyte.host.reconcile._reconcile_pull_requests'),
            patch('holophyte.host.supervisor.linear_budget_low', return_value=False),
        ):
            yield

    def test_released_pass_probes_with_the_loop_units_environment_file(self):
        import store

        self.fixture(
            "ok = bool(os.environ.get('HOLO_FIXTURE_CREDENTIAL'))\n"
            "print('ready' if ok else 'credential missing')\n"
            "raise SystemExit(0 if ok else 1)\n",
            "HOLO_FIXTURE_CREDENTIAL=loop-only\n")
        with self.sweep_environment():
            store.hold(self.conn, self.project_id, "draining for a route change")
            self.one_pass(T0)
            self.assertFalse(self.starts.exists())
            self.assertFalse(self.calls.exists())
            store.release_hold(self.conn, self.project_id, "route changed")
            self.one_pass(T0 + 60_000)
            self.assertNotIn("HOLO_FIXTURE_CREDENTIAL", os.environ)
        self.assertEqual(self.count("launch_backoff"), 0)
        self.assertEqual(self.started(), ["--user start holophyte-loop@repo"])
        self.assertEqual(self.count("launch_loop"), 1)

    def test_a_credential_in_neither_environment_still_backs_off(self):
        self.fixture(
            "ok = bool(os.environ.get('HOLO_FIXTURE_CREDENTIAL'))\n"
            "print('ready' if ok else 'credential missing')\n"
            "raise SystemExit(0 if ok else 1)\n",
            "UNRELATED_SETTING=1\n")
        with self.sweep_environment():
            self.one_pass(T0)
        self.assertEqual(self.count("launch_backoff"), 1)
        self.assertEqual(self.count("launch_loop"), 0)
        self.assertEqual(self.started(), [])
        reason = self.conn.execute(
            "SELECT launchBackoffReason FROM projects").fetchone()[0]
        self.assertIn("credential missing", reason)

    def test_a_route_echoing_the_files_value_is_redacted(self):
        secret = "loop-file-secret-7c41e9"
        self.fixture(
            "print('denied for', os.environ.get('HOLO_FIXTURE_CREDENTIAL'))\n"
            "raise SystemExit(1)\n",
            f'HOLO_FIXTURE_CREDENTIAL="{secret}"\n')
        out = io.StringIO()
        with self.sweep_environment():
            self.one_pass(T0, out)
        events = " ".join(row[0] for row in self.conn.execute(
            "SELECT summary FROM runEvents"))
        reason = self.conn.execute(
            "SELECT launchBackoffReason FROM projects").fetchone()[0]
        self.assertNotIn(secret, out.getvalue())
        self.assertNotIn(secret, events)
        self.assertIn("[redacted]", events)
        self.assertIn("denied for [redacted]", reason)
        self.assertEqual(self.started(), [])

    def test_a_file_systemd_would_read_is_read_past_its_bad_lines(self):
        self.fixture(
            "ok = os.environ.get('HOLO_FIXTURE_CREDENTIAL') == 'loop-only'\n"
            "print('ready' if ok else 'credential missing')\n"
            "raise SystemExit(0 if ok else 1)\n",
            "# a comment = with an equals sign\n; another = one\n"
            "a line with no assignment\nBROKEN=a\x00b\n"
            "  HOLO_FIXTURE_CREDENTIAL = loop-only  \n")
        with self.sweep_environment():
            self.one_pass(T0)
        self.assertEqual(self.count("launch_backoff"), 0)
        self.assertEqual(self.started(), ["--user start holophyte-loop@repo"])

    def test_an_overlay_refused_part_way_leaves_no_file_value_behind(self):
        self.fixture("print('ready')\n", "HOLO_FIXTURE_CREDENTIAL=loop-only\n")
        values = {"HOLO_FIXTURE_CREDENTIAL": "loop-only",
                  "SECOND": "also-loop-only", "BROKEN": "a\x00b"}
        with self.sweep_environment(), patch(
                "holophyte.host.registry.unit_environment", return_value=values):
            os.environ["SECOND"] = "the sweep's own"
            with self.assertRaises(ValueError):
                self.one_pass(T0)
            self.assertNotIn("HOLO_FIXTURE_CREDENTIAL", os.environ)
            self.assertEqual(os.environ["SECOND"], "the sweep's own")
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.started(), [])

    def test_the_container_probe_gets_a_holophyte_credential_but_no_target_key(self):
        from holophyte.isolation import launcher
        from holophyte.redact import redact_values

        self.fixture(
            "print('ready')\n",
            "HOLOPHYTE_TARGET=/another/project\nHOLOPHYTE_SERVE_PORT=9999\n"
            "HOLOPHYTE_FIXTURE_CREDENTIAL=loop-only\n"
            "UNIT_SETTING=plain-unit-setting\n",
            'implementer_isolation = "container"\n'
            'implementer_credential = { env = "HOLOPHYTE_FIXTURE_CREDENTIAL" }\n')
        seen = []

        def container(argv, cwd, timeout, env, **kwargs):
            seen.append((env.get("HOLOPHYTE_FIXTURE_CREDENTIAL"),
                         os.environ.get("HOLOPHYTE_TARGET"),
                         os.environ.get("HOLOPHYTE_SERVE_PORT")))
            ok = env.get("HOLOPHYTE_FIXTURE_CREDENTIAL") == "loop-only"
            return (0, "ready") if ok else (1, "credential missing")

        with self.sweep_environment(), \
                patch.dict(os.environ, HOLOPHYTE_SERVE_PORT="4242"), \
                patch.object(launcher, "image_ready"), \
                patch.object(launcher.review_runner, "_remove_container"), \
                patch.object(launcher, "run_capped", side_effect=container):
            os.environ.pop("HOLOPHYTE_TARGET", None)
            self.one_pass(T0)
            self.assertNotIn("HOLOPHYTE_FIXTURE_CREDENTIAL", os.environ)
            self.assertEqual(os.environ["HOLOPHYTE_SERVE_PORT"], "4242")
        self.assertEqual(seen, [("loop-only", None, "4242")])
        self.assertEqual(self.count("launch_backoff"), 0)
        self.assertEqual(self.started(), ["--user start holophyte-loop@repo"])
        self.assertEqual(redact_values("plain-unit-setting"), "plain-unit-setting")
