"""A reviewer at capacity falls back, and a stored failure reason stays short."""
import contextlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_fixture import SweepTestCase  # noqa: E402

import review_runner  # noqa: E402
import store  # noqa: E402
from holophyte.agents import probes, roles  # noqa: E402
from holophyte.agents.agent_routes import reset  # noqa: E402
from holophyte.loop.gates import InfraFailure, sh  # noqa: E402

CAPACITY = ('{"type":"error","message":"Selected model is at capacity.'
            ' Please try a different model."}')
AT_CAPACITY = (
    'import sys\n'
    'print(\'{"type":"thread.started","thread_id":"t-1"}\')\n'
    'print(\'{"type":"turn.started"}\')\n'
    f'print({CAPACITY!r})\n'
    'print(\'{"type":"turn.failed","error":{"message":"at capacity"}}\')\n'
    'print("PREFLIGHT_OK candidate=" + sys.argv[1], file=sys.stderr)\n'
    'sys.exit(1)\n')


class CapacityFallbackTests(SweepTestCase):
    def setUp(self):
        super().setUp()
        sh(['git', 'init', '-q', str(self.target)])
        sh(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
            'commit', '--allow-empty', '-qm', 'base'], cwd=self.target)
        self.sha = sh(['git', 'rev-parse', 'HEAD'], cwd=self.target)
        self.calls = self.root / 'calls'
        self.addCleanup(reset, self.project)

    def fallback_reviewer(self):
        path = self.root / 'devin-review'
        path.write_text(
            f'#!{sys.executable}\nimport subprocess, sys\n'
            f'with open({str(self.calls)!r}, "a") as f:\n'
            ' f.write(sys.argv[-1] + "\\n")\n'
            f'if sys.argv[-1] == {probes.REVIEW_PROBE_GOAL!r}:\n'
            ' print("ready", subprocess.check_output(\n'
            '     ["git", "rev-parse", "HEAD"], text=True).strip())\n'
            'else:\n print("VERDICT: PASS")\n')
        path.chmod(0o755)
        return str(path)

    def review(self, run):
        """The container reviewer's model is at capacity: its command exits 1."""
        container = self.root / 'container'
        container.write_text(AT_CAPACITY)
        def at_capacity(*, candidate_sha, prompt, **_):
            review_runner._run([sys.executable, str(container), candidate_sha,
                                prompt])
        with patch.object(roles.review_runner, 'run_review',
                          side_effect=at_capacity), \
                contextlib.redirect_stdout(io.StringIO()):
            return roles.agent(self.project, 'review', 'judge', self.target,
                               base_sha=self.sha, candidate_sha=self.sha,
                               conn=self.conn, run_id=run)

    def switch_events(self, run):
        return [json.loads(row[0]) for row in self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId=? AND kind='route_fallback'",
            (run,))]

    def test_capacity_error_switches_the_review_to_the_fallback_route(self):
        devin = self.fallback_reviewer()
        self.configure(f'[agents]\nreviewer_fallback = ["{devin}"]\n')
        run = self.a_run()
        self.assertEqual(self.review(run), 'VERDICT: PASS')
        self.assertEqual(self.calls.read_text().splitlines(),
                         [probes.REVIEW_PROBE_GOAL, 'judge'])
        switch, = self.switch_events(run)
        self.assertEqual((switch['seat'], switch['command']), ('reviewer', devin))
        self.assertIn('Selected model is at capacity', switch['reason'])

    def test_capacity_error_without_a_fallback_fails_the_review_route(self):
        run = self.a_run()
        with self.assertRaises(InfraFailure) as raised:
            self.review(run)
        self.assertEqual(raised.exception.failure_kind, 'review_route')
        self.assertIn('reviewer route failed for review', str(raised.exception))
        self.assertEqual(self.switch_events(run), [])


class OutcomeReasonBoundTests(SweepTestCase):
    def test_a_megabyte_reason_is_stored_bounded_and_kept_whole_in_events(self):
        first = 'reviewer route failed for review: command failed (1): docker run'
        last = 'PREFLIGHT_OK candidate=861e0a8e'
        reason = '\n'.join([first, *['x' * 99] * 10_000, last])
        self.assertGreaterEqual(len(reason), 1_000_000)
        run = self.a_run()
        store.release(self.conn, run, 'failed', reason, outcome_class='infra',
                      failure_kind='review_route')
        stored, = self.conn.execute(
            'SELECT outcomeReason FROM runs WHERE id=?', (run,)).fetchone()
        self.assertLessEqual(len(stored), 2000)
        self.assertEqual(stored.splitlines()[0], first)
        self.assertEqual(stored.splitlines()[-1], last)
        kept, = self.conn.execute(
            "SELECT payload FROM runEvents WHERE runId=? AND kind='outcome_reason'",
            (run,)).fetchone()
        self.assertEqual(kept, reason)
