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

from holophyte.agents import (  # noqa: E402
    agent_output,
    fallback,
    probes,
    review_workspace,
    roles,
)
from holophyte.agents.agent_routes import reset  # noqa: E402
from holophyte.cli import operator  # noqa: E402


class AgentFallbackTests(SweepTestCase):
    def routes(self, probe_fails=True, fallback_fails=False):
        self.calls = self.root / 'calls'
        for name in ('codex-primary', 'devin-fallback'):
            path = self.root / name
            path.write_text(
                f'#!{sys.executable}\nimport subprocess, sys\n'
                f'with open({str(self.calls)!r}, "a") as f:\n'
                f' f.write({name!r} + " " + sys.argv[-1] + "\\n")\n'
                f'review_probe = sys.argv[-1] == {probes.REVIEW_PROBE_GOAL!r}\n'
                'probe = review_probe or '
                'sys.argv[-1] == "Reply with the single word: ready"\n'
                f'failed = ({probe_fails!r} or not probe) '
                f'if {name!r} == "codex-primary" else {fallback_fails!r}\n'
                'print("ERROR: You\'ve hit your usage limit" if failed else '
                '("ready" if probe else "turn completed"))\n'
                'if review_probe and not failed:\n'
                ' print(subprocess.check_output('
                '["git", "rev-parse", "HEAD"], text=True).strip())\n'
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
            code = operator.main(self.project, SimpleNamespace(team='team-1'))
        return code, out.getvalue()

    def test_failed_writer_probe_continues_on_implementer(self):
        from holophyte.serve.serve_runs import active_routes

        self.routes()
        self.configure(f'[agents]\nimplementer = "{self.fallback}"\n'
                       f'writer = "{self.primary}"\n')
        def turn(*_):
            self.assertEqual(active_routes(self.project)['writer'],
                             {'command': self.fallback, 'fallback': self.fallback})
            self.assertEqual(roles.agent(self.project, 'write', 'describe',
                                          self.target),
                             'turn completed')
            return 0
        code, output = self.start(turn)
        self.assertEqual(code, 0)
        self.assertIn('writer probe failed', output)
        self.assertIn('using implementer', output)
        self.assertEqual(self.calls.read_text().splitlines(), [
            'devin-fallback ' + probes.PROBE_GOAL,
            'codex-primary ' + probes.PROBE_GOAL,
            'devin-fallback describe'])

    def test_writer_status_tracks_implementer_fallback_and_clears(self):
        from holophyte.serve.serve_runs import active_routes

        self.routes()
        self.configure(f'[agents]\nimplementer = "{self.primary}"\n'
                       f'implementer_fallback = "{self.fallback}"\n'
                       f'writer = "{self.primary}"\n')
        self.addCleanup(reset, self.project)
        self.assertTrue(fallback.startup_routes(
            self.project, SimpleNamespace(team='team-1')))
        self.assertEqual(active_routes(self.project)['writer'],
                         {'command': self.fallback, 'fallback': self.fallback})
        # A read-only startup probe must not clear the published substitution.
        Path(self.primary).write_text(f'#!{sys.executable}\nprint("ready")\n')
        probes.probe_writer(self.project, activate=False)
        self.assertEqual(active_routes(self.project)['writer']['command'],
                         self.fallback)
        probes.probe_writer(self.project, activate=True)
        self.assertEqual(active_routes(self.project)['writer'],
                         {'command': self.primary})
        Path(self.primary).write_text(f'#!{sys.executable}\nprint("unavailable")\n')
        probes.probe_writer(self.project, activate=True)
        self.assertEqual(active_routes(self.project)['writer']['command'],
                         self.fallback)
        reset(self.project)
        self.assertEqual(active_routes(self.project)['writer'],
                         {'command': self.primary})

    def test_worker_probes_writer_without_fallback_keys(self):
        from holophyte.loop import pool

        subprocess.run(['git', 'init', '-q', str(self.target)], check=True)
        self.routes()
        self.configure(f'[agents]\nimplementer = "{self.fallback}"\n'
                       f'writer = "{self.primary}"\n[loop]\nworkers = 2\n')
        def turn(*_):
            self.assertEqual(roles.agent(self.project, 'write', 'describe',
                                          self.target),
                             'turn completed')
            return pool.WORKER_PARKED
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(
                pool, '_worker', side_effect=turn):
            code = pool.worker(self.project, SimpleNamespace(team='team-1'))
        self.assertEqual(code, pool.WORKER_PARKED)
        self.assertIn('writer probe failed', out.getvalue())
        self.assertIn('using implementer', out.getvalue())
        self.assertEqual(self.calls.read_text().splitlines(), [
            'devin-fallback ' + probes.PROBE_GOAL,
            'codex-primary ' + probes.PROBE_GOAL,
            'devin-fallback describe'])

    def test_missing_implementer_image_stops_before_claim(self):
        import subprocess

        from holophyte.isolation import launcher

        self.configure('[agents]\nimplementer_isolation = "container"\n'
                       'implementer_image = "missing-implementer:test"\n')
        real_run = subprocess.run
        def docker_missing(argv, **kwargs):
            if argv[:3] == ['docker', 'image', 'inspect']:
                return subprocess.CompletedProcess(argv, 1, '', 'missing')
            return real_run(argv, **kwargs)
        with patch.object(launcher.subprocess, 'run', side_effect=docker_missing):
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
            self.assertEqual(roles.agent(self.project, 'implement', 'do work',
                             self.target, conn=self.conn, run_id=run),
                             'turn completed')
            return 0
        code, output = self.start(turn)
        self.assertEqual(code, 0)
        self.assertIn('implementer route down', output)
        self.assertEqual(self.calls.read_text().splitlines(), [
            'codex-primary ' + probes.PROBE_GOAL,
            'devin-fallback ' + probes.PROBE_GOAL, 'devin-fallback do work'])
        rows = self.conn.execute(
            "SELECT projectId, guidance FROM interventions "
            "WHERE action='route_fallback'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], self.project_id)
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
                self.assertEqual(roles.agent(self.project, 'implement', goal,
                                 self.target, conn=self.conn, run_id=run),
                                 'turn completed')
            return 0
        code, output = self.start(turn)
        self.assertEqual(code, 0)
        self.assertEqual(output.count('using fallback:'), 1)
        self.assertEqual(self.calls.read_text().splitlines(), [
            'codex-primary ' + probes.PROBE_GOAL, 'codex-primary first',
            'devin-fallback ' + probes.PROBE_GOAL, 'devin-fallback first',
            'devin-fallback second'])
        payload, = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId=? "
            "AND kind='route_fallback'", (run,)).fetchone()
        self.assertEqual(json.loads(payload)['reason'],
                         "ERROR: You've hit your usage limit")

    def test_command_credentials_never_reach_fallback_sinks(self):
        from holophyte.serve.serve_runs import active_routes

        self.routes()
        secret = 'command-only-credential'
        self.configure(f'[agents]\nimplementer = "{self.primary}"\n'
                       f'implementer_fallback = "{self.fallback} --api-key {secret}"\n')
        run = self.a_run()
        def turn(*_):
            self.assertEqual(roles.agent(self.project, 'implement', 'work',
                             self.target, conn=self.conn, run_id=run),
                             'turn completed')
            self.assertEqual(active_routes(self.project)['implementer'],
                             {'command': self.fallback, 'fallback': self.fallback})
            for path in self.project.holo_dir.glob('active-routes-*.json'):
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
        probe = probes.ProbeResult(
            command=['codex', 'exec', '--value', 'plain', '--api-key=-secret'],
            returncode=1,
            output=reason, timeout=90)
        diagnostic = probes.probe_diagnostic(self.project, probe)
        out = io.StringIO()
        self.addCleanup(reset, self.project)
        with contextlib.redirect_stdout(out):
            fallback.activate_fallback(self.project, 'implement', reason,
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
            return 0, 'ready' if cmd[-1] == probes.PROBE_GOAL else 'completed'
        with patch.object(roles, 'run_capped', side_effect=execute), \
                patch.object(probes, 'run_capped', side_effect=execute):
            self.addCleanup(reset, self.project)
            result = roles.agent(self.project, 'implement', 'work', self.target,
                                  conn=self.conn, run_id=run)
        self.assertEqual(result, 'completed')
        self.assertEqual([cmd[0] for cmd in dispatched],
                         ['claude', self.fallback, self.fallback])
        self.assertEqual(dispatched[-1][-1], 'work')
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM runEvents WHERE kind='route_fallback'"
        ).fetchone()[0], 1)

    def test_scheduler_readiness_does_not_activate_fallback(self):
        from holophyte.agents.agent_routes import routes

        self.routes()
        def scheduled(*_):
            self.assertEqual(routes(self.project).commands, {})
            self.assertEqual(routes(self.project).pending, {})
            self.assertEqual(list(self.project.holo_dir.glob('active-routes-*.json')),
                             [])
            return 0
        with patch.object(operator, 'loop_config',
                          return_value=SimpleNamespace(workers=2)), \
                patch.object(operator, 'scheduler', side_effect=scheduled):
            self.assertEqual(operator.main(self.project,
                                           SimpleNamespace(team='team-1')), 0)
        self.assertEqual(self.calls.read_text().splitlines(), [
            'codex-primary ' + probes.PROBE_GOAL,
            'devin-fallback ' + probes.PROBE_GOAL])
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
        from holophyte.agents.agent_routes import reset
        from holophyte.loop.gates import sh

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
                self.addCleanup(reset, self.project)
                output = roles.agent(self.project, role, 'judge this', self.target,
                                      base_sha=sha, candidate_sha=sha,
                                      conn=self.conn, run_id=run)
                self.assertEqual(output, 'turn completed')
                self.assertEqual(roles.agent_route(self.project, role), self.fallback)
                self.assertEqual(sh(['git', 'rev-parse',
                                     f'refs/review/{run}/candidate'],
                                    cwd=self.target), sha)
                reset(self.project)
        self.assertEqual(self.calls.read_text().splitlines(), 2 * [
            'codex-primary judge this', 'devin-fallback ' + probes.REVIEW_PROBE_GOAL,
            'devin-fallback judge this'])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM interventions WHERE action='route_fallback'"
        ).fetchone()[0], 2)

    def reviewer_repository(self):
        from holophyte.loop.gates import sh

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
                result = roles.agent(self.project, role, 'judge', self.target,
                                      base_sha=sha, candidate_sha=sha, timeout=1)
                self.assertIsInstance(result, agent_output.AgentOutput)
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
        self.addCleanup(reset, self.project)
        result = roles.agent(self.project, 'review', 'judge', self.target,
                              base_sha=sha, candidate_sha=sha, timeout=1,
                              conn=self.conn, run_id=run)
        self.assertEqual(result, 'turn completed')
        self.assertEqual(self.calls.read_text().splitlines(), [
            'devin-fallback ' + probes.REVIEW_PROBE_GOAL, 'devin-fallback judge'])
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
            result = roles.agent(self.project, 'review', 'judge', self.target,
                                  base_sha=sha, candidate_sha=sha, timeout=5)
        self.assertEqual(result, 'PASS')
        self.assertEqual(printed.getvalue(), '')
        self.assert_review_cleanup()

    def test_review_cleanup_ignores_inherited_git_repository_variables(self):
        from holophyte.loop.gates import sh

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
            with review_workspace.review_scratch(self.target) as scratch:
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
        real_sh = review_workspace.sh
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
                with patch.object(review_workspace, 'sh', side_effect=git_234), \
                        contextlib.redirect_stdout(printed):
                    result = roles.agent(self.project, 'review', 'judge', self.target,
                                          base_sha=sha, candidate_sha=sha, timeout=1)
                self.assertEqual(result.timed_out, sleep)
                if not sleep:
                    self.assertEqual(result, 'PASS')
                self.assertEqual(printed.getvalue(), '')
                self.assert_review_cleanup()

    def test_failed_mid_turn_probe_does_not_redispatch_or_log_switch(self):
        self.routes(probe_fails=False, fallback_fails=True)
        run = self.a_run()
        from holophyte.loop.gates import InfraFailure
        with self.assertRaisesRegex(InfraFailure, 'probe failed'):
            self.start(lambda *_: roles.agent(
                self.project, 'implement', 'first', self.target,
                conn=self.conn, run_id=run))
        self.assertEqual(self.calls.read_text().splitlines(), [
            'codex-primary ' + probes.PROBE_GOAL, 'codex-primary first',
            'devin-fallback ' + probes.PROBE_GOAL])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM interventions WHERE action='route_fallback'"
        ).fetchone()[0], 0)


class ContainerReviewFallbackTests(SweepTestCase):
    PAIRS = ('[agents]\nreview_model = "gpt-6-astra"\nreview_effort = "medium"\n'
             'review_fallback_model = "gpt-5.6-sol"\n'
             'review_fallback_effort = "high"\n')

    def setUp(self):
        from holophyte.loop.gates import sh

        super().setUp()
        sh(['git', 'init', '-q', str(self.target)])
        sh(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
            'commit', '--allow-empty', '-qm', 'base'], cwd=self.target)
        self.sha = sh(['git', 'rev-parse', 'HEAD'], cwd=self.target)
        self.addCleanup(reset, self.project)

    def container(self, down):
        """The review runner, with the models in `down` unable to start."""
        import review_runner

        self.reviews = []
        def run_review(*, candidate_sha, prompt, model, effort, **_):
            self.reviews.append((model, effort, prompt))
            if model in down:
                raise review_runner.ReviewBoundaryError(
                    f'{model}: container produced no events')
            return (f'ready {candidate_sha}' if prompt == probes.REVIEW_PROBE_GOAL
                    else 'VERDICT: PASS')
        return patch.object(roles.review_runner, 'run_review',
                            side_effect=run_review)

    def review(self, run=None):
        return roles.agent(self.project, 'review', 'judge', self.target,
                            base_sha=self.sha, candidate_sha=self.sha,
                            conn=self.conn, run_id=run)

    def switches(self):
        return [json.loads(row[0]) for row in self.conn.execute(
            "SELECT guidance FROM interventions WHERE action='route_fallback'")]

    def test_config_accepts_the_pair_and_refuses_half_or_beside_commands(self):
        from holophyte.config.agent_settings import review_route
        from holophyte.config.checks import check_config

        self.configure(self.PAIRS)
        check_config(self.project)
        self.assertEqual(review_route(self.project, fallback=True),
                         ('gpt-5.6-sol', 'high'))
        self.configure('[agents]\nreview_fallback_model = "gpt-5.6-sol"\n'
                       'review_fallback_effort = "high"\n'
                       'reviewer = "sh -c"\n')
        with self.assertRaisesRegex(SystemExit,
                                    r'review_fallback_\w+ beside \[agents\] '
                                    r'reviewer:'):
            check_config(self.project)
        self.configure('[agents]\nreview_fallback_model = "gpt-5.6-sol"\n')
        with self.assertRaisesRegex(SystemExit, r'review_fallback_model needs '
                                    r'\[agents\] review_fallback_effort'):
            check_config(self.project)

    def test_the_pair_sits_beside_fallback_commands_but_not_a_reviewer(self):
        from holophyte.config.agent_settings import review_route
        from holophyte.config.checks import check_config

        pair_and_fallbacks = ('[agents]\nreview_model = "gpt-5.6-sol"\n'
                              'review_effort = "xhigh"\n'
                              f'reviewer_fallback = "{sys.executable}"\n'
                              f'implementer_fallback = "{sys.executable}"\n')
        self.configure(pair_and_fallbacks)
        check_config(self.project)
        self.assertEqual(review_route(self.project), ('gpt-5.6-sol', 'xhigh'))
        self.configure(pair_and_fallbacks + 'reviewer = "sh -c"\n')
        with self.assertRaisesRegex(SystemExit, r'review_model beside '
                                    r'\[agents\] reviewer:'):
            check_config(self.project)

    def test_an_unset_pair_reviews_on_gpt_6_astra_at_high(self):
        from holophyte.config.agent_settings import review_route

        self.configure(f'[agents]\nreviewer_fallback = "{sys.executable}"\n')
        self.assertEqual(review_route(self.project), ('gpt-6-astra', 'high'))

    def test_startup_switches_to_the_fallback_pair_for_later_turns(self):
        from holophyte.agents.agent_routes import routes

        self.configure(self.PAIRS)
        self.addCleanup(reset, self.project)
        with self.container(down={'gpt-6-astra'}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(fallback.startup_routes(
                self.project, SimpleNamespace(team='team-1')))
            self.assertEqual(routes(self.project).commands,
                             {'review': 'codex-sol-high'})
            self.assertEqual(self.review(self.a_run()), 'VERDICT: PASS')
        self.assertIn('reviewer route down', out.getvalue())
        self.assertEqual(self.reviews, [
            ('gpt-6-astra', 'medium', probes.REVIEW_PROBE_GOAL),
            ('gpt-5.6-sol', 'high', probes.REVIEW_PROBE_GOAL),
            ('gpt-5.6-sol', 'high', 'judge')])
        switch, = self.switches()
        self.assertEqual((switch['seat'], switch['command']),
                         ('reviewer', 'codex-sol-high'))
        self.assertEqual(roles.agent_route(self.project, 'review'),
                         'codex-sol-high')

    def test_boundary_error_mid_run_retries_the_round_on_the_fallback_pair(self):
        from holophyte.loop.gates import InfraFailure

        self.configure(self.PAIRS)
        run = self.a_run()
        with self.container(down={'gpt-6-astra'}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.review(run), 'VERDICT: PASS')
        self.assertEqual(self.reviews, [
            ('gpt-6-astra', 'medium', 'judge'),
            ('gpt-5.6-sol', 'high', probes.REVIEW_PROBE_GOAL),
            ('gpt-5.6-sol', 'high', 'judge')])
        switch, = self.switches()
        self.assertIn('container produced no events', switch['reason'])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM runEvents WHERE runId=? "
            "AND kind='route_fallback'", (run,)).fetchone()[0], 1)

        reset(self.project)
        with self.container(down={'gpt-6-astra', 'gpt-5.6-sol'}), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(InfraFailure) as raised:
            self.review(self.a_run())
        self.assertEqual(raised.exception.failure_kind, 'review_route')
        self.assertEqual(len(self.switches()), 1)

    def test_startup_probe_stages_without_the_worktree_carry(self):
        import tempfile

        import review_runner

        (self.target / '.gitignore').write_text('.venv/\n')
        (self.target / '.venv').mkdir()
        self.configure(self.PAIRS + '[worktree]\ncarry = [".venv"]\n')
        staged = []
        def run_review(*, repo, base_sha, candidate_sha, carry=(), **_):
            with tempfile.TemporaryDirectory() as root:
                review_runner.stage_candidate(repo, Path(root) / 'stage', base_sha,
                                              candidate_sha, carry=carry)
            staged.append(candidate_sha)
            return f'ready {candidate_sha}'
        with patch.object(roles.review_runner, 'run_review',
                          side_effect=run_review), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(fallback.startup_routes(
                self.project, SimpleNamespace(team='team-1')))
        self.assertEqual(len(staged), 1)
        self.assertEqual(self.switches(), [])

    def test_without_the_pair_startup_skips_the_reviewer_and_turns_stay_primary(self):
        from holophyte.loop.gates import InfraFailure

        self.configure('[agents]\nreview_model = "gpt-6-astra"\n'
                       'review_effort = "medium"\n')
        with self.container(down=set()), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(fallback.startup_routes(
                self.project, SimpleNamespace(team='team-1')))
            self.assertEqual(self.reviews, [])
            self.assertEqual(self.review(self.a_run()), 'VERDICT: PASS')
        with self.container(down={'gpt-6-astra'}), \
                self.assertRaises(InfraFailure) as raised:
            self.review(self.a_run())
        self.assertEqual(raised.exception.failure_kind, 'review_route')
        self.assertEqual(self.reviews, [('gpt-6-astra', 'medium', 'judge')])
        self.assertEqual(self.switches(), [])

class ReviewerFallbackListTests(SweepTestCase):
    def setUp(self):
        from holophyte.loop.gates import sh

        super().setUp()
        sh(['git', 'init', '-q', str(self.target)])
        sh(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
            'commit', '--allow-empty', '-qm', 'base'], cwd=self.target)
        self.sha = sh(['git', 'rev-parse', 'HEAD'], cwd=self.target)
        self.calls = self.root / 'calls'
        self.addCleanup(reset, self.project)

    def reviewer(self, name, down=False):
        path = self.root / name
        path.write_text(
            f'#!{sys.executable}\nimport subprocess, sys\n'
            f'with open({str(self.calls)!r}, "a") as f:\n'
            f' f.write({name!r} + " " + sys.argv[-1] + "\\n")\n'
            f'if {down!r}:\n sys.exit("Quota exhausted")\n'
            f'if sys.argv[-1] == {probes.REVIEW_PROBE_GOAL!r}:\n'
            ' print("ready", subprocess.check_output(\n'
            '     ["git", "rev-parse", "HEAD"], text=True).strip())\n'
            'else:\n print("VERDICT: PASS")\n')
        path.chmod(0o755)
        return str(path)

    def configure_fallback(self, value):
        self.configure(f'[agents]\nreviewer_fallback = {value}\n')

    def start(self):
        """Startup with the container reviewer unable to start."""
        import review_runner

        down = review_runner.ReviewBoundaryError('container produced no events')
        with patch.object(roles.review_runner, 'run_review', side_effect=down), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            started = fallback.startup_routes(self.project,
                                            SimpleNamespace(team='team-1'))
        return started, out.getvalue()

    def review(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return roles.agent(self.project, 'review', 'judge', self.target,
                                base_sha=self.sha, candidate_sha=self.sha,
                                conn=self.conn, run_id=self.a_run())

    def switches(self):
        return [json.loads(row[0]) for row in self.conn.execute(
            "SELECT guidance FROM interventions WHERE action='route_fallback'")]

    def test_a_list_switches_to_its_first_entry_whose_probe_passes(self):
        from holophyte.agents.agent_routes import routes
        from holophyte.config.checks import check_config

        devin = self.reviewer('devin-review', down=True)
        claude = self.reviewer('claude-review')
        self.configure_fallback(f'["{devin}", "{claude}"]')
        check_config(self.project)
        started, output = self.start()
        self.assertTrue(started)
        self.assertEqual(routes(self.project).commands, {'review': claude})
        self.assertEqual(self.review(), 'VERDICT: PASS')
        self.assertIn('Quota exhausted', output)
        self.assertEqual(self.calls.read_text().splitlines(), [
            'devin-review ' + probes.REVIEW_PROBE_GOAL,
            'claude-review ' + probes.REVIEW_PROBE_GOAL,
            'claude-review judge'])
        switch, = self.switches()
        self.assertEqual((switch['seat'], switch['command']), ('reviewer', claude))

    def test_a_list_whose_every_probe_fails_ends_as_one_failed_fallback(self):
        from holophyte.agents.agent_routes import routes

        first = self.reviewer('devin-review', down=True)
        second = self.reviewer('claude-review', down=True)
        for value, probed in ((f'"{first}"', ['devin-review']),
                              (f'["{first}", "{second}"]',
                               ['devin-review', 'claude-review'])):
            with self.subTest(reviewer_fallback=value):
                self.calls.unlink(missing_ok=True)
                self.configure_fallback(value)
                started, output = self.start()
                self.assertFalse(started)
                self.assertEqual(routes(self.project).commands, {})
                self.assertEqual(self.switches(), [])
                self.assertEqual(self.calls.read_text().splitlines(), [
                    f'{name} {probes.REVIEW_PROBE_GOAL}' for name in probed])
                self.assertEqual(output.count('Quota exhausted'), len(probed))

    def test_a_string_still_switches_and_a_malformed_list_is_refused(self):
        from holophyte.agents.agent_routes import routes
        from holophyte.config.checks import check_config

        devin = self.reviewer('devin-review')
        self.configure_fallback(f'"{devin}"')
        check_config(self.project)
        self.assertTrue(self.start()[0])
        self.assertEqual(routes(self.project).commands, {'review': devin})
        self.assertEqual(self.review(), 'VERDICT: PASS')
        for value in ('[]', f'["{devin}", 3]', f'["{devin}", ""]'):
            with self.subTest(reviewer_fallback=value):
                self.configure_fallback(value)
                with self.assertRaisesRegex(SystemExit,
                                            r'\[agents\] reviewer_fallback must'):
                    check_config(self.project)


class FailedRouteLoopTests(LoopFixture):
    def test_failed_fallback_probe_stops_even_when_work_failures_continue(self):
        primary = self.db.parent / 'codex-primary'
        primary.write_text(
            f'#!{sys.executable}\nimport sys\n'
            f'probe = sys.argv[-1] == {probes.PROBE_GOAL!r}\n'
            'print("ready" if probe else "You\'ve hit your usage limit")\n'
            'sys.exit(0 if probe else 1)\n')
        primary.chmod(0o755)
        self.configure(f'[agents]\nimplementer = "{primary}"\n'
                       'implementer_fallback = "false"\n'
                       '[loop]\nstop_on_failure = false\n')
        provider = StubProvider(a_task(1), a_task(2))
        with no_agent_processes(), patch.dict(sys.modules,
                                              {'linear_provider': provider}):
            code = operator.main(self.project, provider)
        self.assertEqual(code, 1)
        self.assertEqual(self.read('SELECT count(*) FROM runs'), [(1,)])
        self.assertEqual(self.read(
            "SELECT count(*) FROM interventions WHERE action='route_fallback'"),
            [(0,)])
