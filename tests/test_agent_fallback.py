"""Fallback routes cross real process boundaries and leave store evidence."""
import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
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

    def test_failed_writer_probe_continues_on_implementer(self):
        self.routes()
        self.configure(f'[agents]\nimplementer = "{self.fallback}"\n'
                       f'writer = "{self.primary}"\n')
        def turn(*_):
            self.assertEqual(agents.agent(self.tgt, 'write', 'describe', self.target),
                             'turn completed')
            return 0
        code, output = self.start(turn)
        self.assertEqual(code, 0)
        self.assertIn('writer probe failed', output)
        self.assertIn('using implementer', output)
        self.assertEqual(self.calls.read_text().splitlines(), [
            'devin-fallback ' + agents.PROBE_GOAL,
            'codex-primary ' + agents.PROBE_GOAL,
            'devin-fallback describe'])

    def test_worker_probes_writer_without_fallback_keys(self):
        from holophyte import pool

        subprocess.run(['git', 'init', '-q', str(self.target)], check=True)
        self.routes()
        self.configure(f'[agents]\nimplementer = "{self.fallback}"\n'
                       f'writer = "{self.primary}"\n[loop]\nworkers = 2\n')
        def turn(*_):
            self.assertEqual(agents.agent(self.tgt, 'write', 'describe', self.target),
                             'turn completed')
            return pool.WORKER_PARKED
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(
                pool, '_worker', side_effect=turn):
            code = pool.worker(self.tgt, SimpleNamespace(team='team-1'))
        self.assertEqual(code, pool.WORKER_PARKED)
        self.assertIn('writer probe failed', out.getvalue())
        self.assertIn('using implementer', out.getvalue())
        self.assertEqual(self.calls.read_text().splitlines(), [
            'devin-fallback ' + agents.PROBE_GOAL,
            'codex-primary ' + agents.PROBE_GOAL,
            'devin-fallback describe'])

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

    def reviewer_repository(self):
        from holophyte.gates import sh

        sh(['git', 'init', '-q', str(self.target)])
        sh(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
            'commit', '--allow-empty', '-qm', 'base'], cwd=self.target)
        return sh(['git', 'rev-parse', 'HEAD'], cwd=self.target)

    def scratch_reviewer(self, *, sleep=False, checkout='checkout with spaces'):
        path = self.root / 'scratch-reviewer'
        self.evidence = self.root / 'review-evidence.json'
        path.write_text(
            f'#!{sys.executable}\n'
            'import json, os, pathlib, subprocess, sys, time\n'
            'scratch = pathlib.Path(os.environ["HOLOPHYTE_REVIEW_SCRATCH"])\n'
            f'checkout = scratch / {checkout!r}\n'
            'subprocess.run(["git", "worktree", "add", "--detach", '
            'str(checkout), "HEAD"], check=True, capture_output=True)\n'
            'child = subprocess.Popen([sys.executable, "-c", '
            '"import time; time.sleep(60)"])\n'
            f'pathlib.Path({str(self.evidence)!r}).write_text(json.dumps('
            '{"scratch": str(scratch), "pids": [os.getpid(), child.pid]}))\n'
            + ('subprocess.run(["git", "worktree", "lock", str(checkout)], '
               'check=True, capture_output=True)\ntime.sleep(60)\n' if sleep else
               'child.terminate()\nchild.wait()\n'
               'subprocess.run(["git", "worktree", "remove", str(checkout)], '
               'check=True, capture_output=True)\nprint("PASS")\n'))
        path.chmod(0o755)
        return path

    def assert_review_cleanup(self):
        evidence = json.loads(self.evidence.read_text())
        self.assertFalse(Path(evidence['scratch']).exists())
        listed = subprocess.check_output(
            ['git', 'worktree', 'list', '--porcelain'], cwd=self.target, text=True)
        self.assertNotIn(evidence['scratch'], listed)
        for pid in evidence['pids']:
            # A killed orphan may await init's waitpid; a zombie is not alive.
            stat = Path(f'/proc/{pid}/stat')
            self.assertTrue(not stat.exists() or
                            stat.read_text().split(')')[-1].split()[0] == 'Z',
                            f'reviewer process {pid} still alive')

    def kill_review_leftovers(self):
        if self.evidence.exists():
            for pid in json.loads(self.evidence.read_text())['pids']:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_timeout_reaps_review_seats_and_scratch_worktree(self):
        sha = self.reviewer_repository()
        command = self.scratch_reviewer(
            sleep=True, checkout='quoted " café\\name\n\ncheckout')
        self.addCleanup(self.kill_review_leftovers)
        for role, seat in (('review', 'reviewer'), ('adjudicate', 'adjudicator')):
            with self.subTest(role=role):
                self.configure(f'[agents]\n{seat} = "{command}"\n')
                result = agents.agent(self.tgt, role, 'judge', self.target,
                                      base_sha=sha, candidate_sha=sha, timeout=1)
                self.assertIsInstance(result, agents.AgentOutput)
                self.assertIn(f'{seat} timed out after', result)
                self.assertIn('minutes', result)
                self.assert_review_cleanup()

    def test_timeout_probes_records_and_retries_fallback(self):
        self.routes()
        sha = self.reviewer_repository()
        command = self.scratch_reviewer(sleep=True)
        self.addCleanup(self.kill_review_leftovers)
        run = self.a_run()
        self.configure(f'[agents]\nreviewer = "{command}"\n'
                       f'reviewer_fallback = "{self.fallback}"\n')
        self.addCleanup(reset, self.tgt)
        result = agents.agent(self.tgt, 'review', 'judge', self.target,
                              base_sha=sha, candidate_sha=sha, timeout=1,
                              conn=self.conn, run_id=run)
        self.assertEqual(result, 'turn completed')
        self.assertEqual(self.calls.read_text().splitlines(), [
            'devin-fallback ' + agents.PROBE_GOAL, 'devin-fallback judge'])
        rows = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId=? AND kind='route_fallback'",
            (run,)).fetchall()
        self.assertEqual(len(rows), 1)
        evidence = json.loads(rows[0][0])
        self.assertIn('reviewer timed out after', evidence['reason'])
        self.assertEqual(evidence['command'], self.fallback)
        self.assert_review_cleanup()

    def test_successful_reviewer_can_remove_own_worktree(self):
        sha = self.reviewer_repository()
        command = self.scratch_reviewer()
        self.addCleanup(self.kill_review_leftovers)
        self.configure(f'[agents]\nreviewer = "{command}"\n')
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            result = agents.agent(self.tgt, 'review', 'judge', self.target,
                                  base_sha=sha, candidate_sha=sha, timeout=5)
        self.assertEqual(result, 'PASS')
        self.assertEqual(printed.getvalue(), '')
        self.assert_review_cleanup()

    def test_review_cleanup_ignores_inherited_git_repository_variables(self):
        from holophyte.gates import sh

        self.reviewer_repository()
        other = self.root / 'other-repository'
        sh(['git', 'init', '-q', str(other)])
        sh(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
            'commit', '--allow-empty', '-qm', 'other'], cwd=other)
        stale = self.root / 'other-checkout'
        sh(['git', 'worktree', 'add', '--detach', str(stale)], cwd=other)
        shutil.rmtree(stale)
        before = sh(['git', 'worktree', 'list', '--porcelain'], cwd=other)
        polluted = {
            'GIT_DIR': str(other / '.git'),
            'GIT_COMMON_DIR': str(other / '.git'),
            'GIT_WORK_TREE': str(other),
            'GIT_INDEX_FILE': str(other / '.git' / 'index'),
        }
        printed = io.StringIO()
        with patch.dict(os.environ, polluted), contextlib.redirect_stdout(printed):
            with agents.review_scratch(self.target) as scratch:
                # Create in the intended repository before exercising cleanup
                # under the inherited environment pointing at another repo.
                with patch.dict(os.environ):
                    for key in polluted:
                        os.environ.pop(key)
                    checkout = scratch / 'locked checkout'
                    sh(['git', 'worktree', 'add', '--detach', str(checkout)],
                       cwd=self.target)
                    sh(['git', 'worktree', 'lock', str(checkout)], cwd=self.target)
        self.assertFalse(scratch.exists())
        self.assertNotIn(str(scratch), sh(
            ['git', 'worktree', 'list', '--porcelain'], cwd=self.target))
        self.assertEqual(sh(['git', 'worktree', 'list', '--porcelain'], cwd=other),
                         before)
        self.assertEqual(printed.getvalue(), '')

    def test_review_cleanup_with_git_234_porcelain(self):
        sha = self.reviewer_repository()
        real_sh = agents.sh
        checkout = 'quoted " café\\name\n\ncheckout'

        def git_234(argv, **kwargs):
            if argv[:3] == ['git', 'worktree', 'list']:
                if '-z' in argv:
                    raise RuntimeError("git worktree list: unknown switch `z'")
                # Git 2.34 emits raw paths, including embedded newlines.
                listing = (f'worktree {self.target}\nHEAD {sha}\n'
                           'branch refs/heads/main\n\n')
                scratch = Path(json.loads(self.evidence.read_text())['scratch'])
                if (scratch / checkout).exists():
                    listing += (f'worktree {scratch / checkout}\nHEAD {sha}\n'
                                'detached\nlocked\n\n')
                return listing
            return real_sh(argv, **kwargs)

        for sleep in (False, True):
            with self.subTest(timeout=sleep):
                command = self.scratch_reviewer(sleep=sleep, checkout=checkout)
                self.addCleanup(self.kill_review_leftovers)
                self.configure(f'[agents]\nreviewer = "{command}"\n')
                printed = io.StringIO()
                with patch.object(agents, 'sh', side_effect=git_234), \
                        contextlib.redirect_stdout(printed):
                    result = agents.agent(self.tgt, 'review', 'judge', self.target,
                                          base_sha=sha, candidate_sha=sha, timeout=1)
                self.assertEqual(result.timed_out, sleep)
                if not sleep:
                    self.assertEqual(result, 'PASS')
                self.assertEqual(printed.getvalue(), '')
                self.assert_review_cleanup()

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
