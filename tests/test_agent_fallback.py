"""Fallback routes cross real process boundaries and leave store evidence."""
import contextlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loop_fixture import (  # noqa: E402 - fixture shares the tests import path
    LoopFixture,
    StubProvider,
    a_task,
    no_agent_processes,
)
from sweep_fixture import SweepTestCase  # noqa: E402

from holophyte import agents, operator  # noqa: E402
from holophyte.agent_routes import reset  # noqa: E402


class AgentFallbackTests(SweepTestCase):
    def routes(self, probe_fails=True, fallback_fails=False):
        self.calls = self.root / 'calls'
        for name in ('codex-primary', 'devin-fallback'):
            path = self.root / name
            path.write_text(
                f'#!{sys.executable}\nimport sys\n'
                f'with open({str(self.calls)!r}, "a") as f:\n'
                f' f.write({name!r} + " " + sys.argv[-1] + "\\n")\n'
                'probe = sys.argv[-1] == "Reply with the single word: ready"\n'
                f'failed = ({probe_fails!r} or not probe) '
                f'if {name!r} == "codex-primary" else {fallback_fails!r}\n'
                'print("ERROR: You\'ve hit your usage limit" if failed else '
                '("ready" if probe else "turn completed"))\n'
                'sys.exit(1 if failed else 0)\n')
            path.chmod(0o755)
        self.primary = str(self.root / 'codex-primary')
        self.fallback = str(self.root / 'devin-fallback')
        self.configure(f'[agents]\nimplementer = "{self.primary}"\n'
                       f'implementer_fallback = "{self.fallback}"\n')

    def start(self, turn):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(
                operator, '_serial', side_effect=turn):
            code = operator.main(self.tgt, SimpleNamespace(team='team-1'))
        return code, out.getvalue()

    def test_missing_implementer_image_stops_before_claim(self):
        import subprocess

        from holophyte import isolation

        self.configure('[agents]\nimplementer_isolation = "container"\n'
                       'implementer_image = "missing-implementer:test"\n')
        real_run = subprocess.run
        def docker_missing(argv, **kwargs):
            if argv[:3] == ['docker', 'image', 'inspect']:
                return subprocess.CompletedProcess(argv, 1, '', 'missing')
            return real_run(argv, **kwargs)
        with patch.object(isolation.subprocess, 'run', side_effect=docker_missing):
            code, output = self.start(
                lambda *_: self.fail('claimed after missing image'))
        self.assertEqual(code, 1)
        self.assertIn('missing-implementer:test', output)
        self.assertIn('docker build -t missing-implementer:test', output)
        self.assertEqual(self.conn.execute(
            'SELECT count(*) FROM runs').fetchone()[0], 0)

    def test_probe_fallback_is_logged_and_first_turn_uses_it(self):
        self.routes()
        run = self.a_run()
        def turn(*_):
            self.assertEqual(agents.agent(self.tgt, 'implement', 'do work',
                             self.target, conn=self.conn, run_id=run),
                             'turn completed')
            return 0
        code, output = self.start(turn)
        self.assertEqual(code, 0)
        self.assertIn('implementer route down', output)
        self.assertEqual(self.calls.read_text().splitlines(), [
            'codex-primary ' + agents.PROBE_GOAL,
            'devin-fallback ' + agents.PROBE_GOAL, 'devin-fallback do work'])
        rows = self.conn.execute(
            "SELECT projectId, guidance FROM interventions "
            "WHERE action='route_fallback'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], self.project)
        evidence = json.loads(rows[0][1])
        self.assertEqual(evidence['seat'], 'implementer')
        self.assertEqual(evidence['command'], self.fallback)
        self.assertIn("You've hit your usage limit", evidence['reason'])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM runEvents WHERE runId=? "
            "AND kind='route_fallback'", (run,)).fetchone()[0], 1)

    def test_quota_retries_once_and_stays_on_fallback(self):
        self.routes(probe_fails=False)
        run = self.a_run()
        def turn(*_):
            for goal in ('first', 'second'):
                self.assertEqual(agents.agent(self.tgt, 'implement', goal,
                                 self.target, conn=self.conn, run_id=run),
                                 'turn completed')
            return 0
        code, output = self.start(turn)
        self.assertEqual(code, 0)
        self.assertEqual(output.count('using fallback:'), 1)
        self.assertEqual(self.calls.read_text().splitlines(), [
            'codex-primary ' + agents.PROBE_GOAL, 'codex-primary first',
            'devin-fallback ' + agents.PROBE_GOAL, 'devin-fallback first',
            'devin-fallback second'])
        payload, = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId=? "
            "AND kind='route_fallback'", (run,)).fetchone()
        self.assertEqual(json.loads(payload)['reason'],
                         "ERROR: You've hit your usage limit")

    def test_command_credentials_never_reach_fallback_sinks(self):
        from holophyte.serve_runs import active_routes

        self.routes()
        secret = 'command-only-credential'
        self.configure(f'[agents]\nimplementer = "{self.primary}"\n'
                       f'implementer_fallback = "{self.fallback} --api-key {secret}"\n')
        run = self.a_run()
        def turn(*_):
            self.assertEqual(agents.agent(self.tgt, 'implement', 'work',
                             self.target, conn=self.conn, run_id=run),
                             'turn completed')
            self.assertEqual(active_routes(self.tgt)['implementer'],
                             {'command': self.fallback, 'fallback': self.fallback})
            for path in self.tgt.holo_dir.glob('active-routes-*.json'):
                self.assertNotIn(secret, path.read_text())
            return 0
        code, output = self.start(turn)
        self.assertEqual(code, 0)
        self.assertNotIn(secret, output)
        for query in ('SELECT guidance FROM interventions'
                      " WHERE action != 'migrate'",
                      "SELECT summary FROM runEvents"):
            self.assertNotIn(secret, str(self.conn.execute(query).fetchall()))

    def test_command_arguments_do_not_corrupt_diagnostic_words(self):
        self.routes()
        self.configure('[agents]\nimplementer = "codex exec --value plain '
                       '--api-key=-secret"\n'
                       f'implementer_fallback = "{self.fallback}"\n')
        run = self.a_run()
        reason = ("ERROR: You've hit your usage limit; execution failed; "
                  "explanation: argument 'plain', command: codex exec; "
                  "--api-key rejected '-secret'")
        probe = agents.ProbeResult(
            command=['codex', 'exec', '--value', 'plain', '--api-key=-secret'],
            returncode=1,
            output=reason, timeout=90)
        diagnostic = agents.probe_diagnostic(self.tgt, probe)
        out = io.StringIO()
        self.addCleanup(reset, self.tgt)
        with contextlib.redirect_stdout(out):
            agents.activate_fallback(self.tgt, 'implement', reason,
                                     self.conn, run)
        evidence = [diagnostic, out.getvalue()]
        for query, column in (('SELECT guidance FROM interventions'
                               " WHERE action != 'migrate'", 'reason'),
                              ("SELECT summary FROM runEvents "
                               "WHERE kind='route_fallback'", 'reason')):
            evidence.extend(json.loads(row[0])[column]
                            for row in self.conn.execute(query))
        self.assertEqual(len(evidence), 4)
        for text in evidence:
            self.assertIn('execution failed; explanation:', text)
            self.assertNotIn("'plain'", text)
            self.assertNotIn('codex exec', text)
            self.assertNotIn('-secret', text)
            self.assertIn('--api-key', text)

    def test_default_claude_quota_dispatches_fallback(self):
        self.routes()
        self.configure(f'[agents]\nimplementer_fallback = "{self.fallback}"\n')
        run = self.a_run()
        dispatched = []
        def execute(cmd, *_args, **_kwargs):
            dispatched.append(cmd)
            if cmd[0] == 'claude':
                return 1, "You've hit your limit · resets tomorrow"
            return 0, 'ready' if cmd[-1] == agents.PROBE_GOAL else 'completed'
        with patch.object(agents, 'run_capped', side_effect=execute):
            self.addCleanup(reset, self.tgt)
            result = agents.agent(self.tgt, 'implement', 'work', self.target,
                                  conn=self.conn, run_id=run)
        self.assertEqual(result, 'completed')
        self.assertEqual([cmd[0] for cmd in dispatched],
                         ['claude', self.fallback, self.fallback])
        self.assertEqual(dispatched[-1][-1], 'work')
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM runEvents WHERE kind='route_fallback'"
        ).fetchone()[0], 1)

    def test_scheduler_readiness_does_not_activate_fallback(self):
        from holophyte.agent_routes import routes

        self.routes()
        def scheduled(*_):
            self.assertEqual(routes(self.tgt).commands, {})
            self.assertEqual(routes(self.tgt).pending, {})
            self.assertEqual(list(self.tgt.holo_dir.glob('active-routes-*.json')), [])
            return 0
        with patch.object(operator, 'loop_config',
                          return_value=SimpleNamespace(workers=2)), \
                patch.object(operator, 'scheduler', side_effect=scheduled):
            self.assertEqual(operator.main(self.tgt, SimpleNamespace(team='team-1')), 0)
        self.assertEqual(self.calls.read_text().splitlines(), [
            'codex-primary ' + agents.PROBE_GOAL,
            'devin-fallback ' + agents.PROBE_GOAL])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM interventions WHERE action='route_fallback'"
        ).fetchone()[0], 0)

    def test_failed_fallback_stops_without_switch_record(self):
        self.routes(fallback_fails=True)
        code, output = self.start(lambda *_: self.fail('claimed after failure'))
        self.assertEqual(code, 1)
        self.assertIn('implementer probe failed', output)
        self.assertNotIn('using fallback:', output)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM interventions WHERE action='route_fallback'"
        ).fetchone()[0], 0)

    def test_review_seats_retry_with_exact_refs_and_goal(self):
        from holophyte.agent_routes import reset
        from holophyte.gates import sh

        self.routes(probe_fails=False)
        sh(['git', 'init', '-q', str(self.target)])
        sh(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
            'commit', '--allow-empty', '-qm', 'base'], cwd=self.target)
        sha = sh(['git', 'rev-parse', 'HEAD'], cwd=self.target)
        run = self.a_run()
        for role, seat in (('review', 'reviewer'), ('adjudicate', 'adjudicator')):
            with self.subTest(seat=seat):
                self.configure(f'[agents]\n{seat} = "{self.primary}"\n'
                               f'{seat}_fallback = "{self.fallback}"\n')
                self.addCleanup(reset, self.tgt)
                output = agents.agent(self.tgt, role, 'judge this', self.target,
                                      base_sha=sha, candidate_sha=sha,
                                      conn=self.conn, run_id=run)
                self.assertEqual(output, 'turn completed')
                self.assertEqual(agents.agent_route(self.tgt, role), self.fallback)
                self.assertEqual(sh(['git', 'rev-parse',
                                     f'refs/review/{run}/candidate'],
                                    cwd=self.target), sha)
                reset(self.tgt)
        self.assertEqual(self.calls.read_text().splitlines(), 2 * [
            'codex-primary judge this', 'devin-fallback ' + agents.PROBE_GOAL,
            'devin-fallback judge this'])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM interventions WHERE action='route_fallback'"
        ).fetchone()[0], 2)

    def test_failed_mid_turn_probe_does_not_redispatch_or_log_switch(self):
        self.routes(probe_fails=False, fallback_fails=True)
        run = self.a_run()
        from holophyte.gates import InfraFailure
        with self.assertRaisesRegex(InfraFailure, 'probe failed'):
            self.start(lambda *_: agents.agent(
                self.tgt, 'implement', 'first', self.target,
                conn=self.conn, run_id=run))
        self.assertEqual(self.calls.read_text().splitlines(), [
            'codex-primary ' + agents.PROBE_GOAL, 'codex-primary first',
            'devin-fallback ' + agents.PROBE_GOAL])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM interventions WHERE action='route_fallback'"
        ).fetchone()[0], 0)


class FailedRouteLoopTests(LoopFixture):
    def test_failed_fallback_probe_stops_even_when_work_failures_continue(self):
        primary = self.db.parent / 'codex-primary'
        primary.write_text(
            f'#!{sys.executable}\nimport sys\n'
            f'probe = sys.argv[-1] == {agents.PROBE_GOAL!r}\n'
            'print("ready" if probe else "You\'ve hit your usage limit")\n'
            'sys.exit(0 if probe else 1)\n')
        primary.chmod(0o755)
        self.configure(f'[agents]\nimplementer = "{primary}"\n'
                       'implementer_fallback = "false"\n'
                       '[loop]\nstop_on_failure = false\n')
        provider = StubProvider(a_task(1), a_task(2))
        with no_agent_processes(), patch.dict(sys.modules,
                                              {'linear_provider': provider}):
            code = operator.main(self.tgt, provider)
        self.assertEqual(code, 1)
        self.assertEqual(self.read('SELECT count(*) FROM runs'), [(1,)])
        self.assertEqual(self.read(
            "SELECT count(*) FROM interventions WHERE action='route_fallback'"),
            [(0,)])
