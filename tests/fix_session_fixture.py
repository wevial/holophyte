"""Fix-session regression cases, exercised by the factory loop/config suites."""
import json
from pathlib import Path
from unittest.mock import patch

from fake_agent import APPROVE, REQUEST_CHANGES, Commit, FakeAgent
from loop_fixture import VALID_BODY, StubProvider, a_task

import holophyte.agents as agents
import holophyte.loop as loop


class FixSessionCases:
    def exercise_fix(self, mode='resume', session=True, fallback=False,
                     fail=False, cap_retry=False):
        config = ('[agents]\nimplementer = "primary-cli"\n'
                  'implementer_resume = "resume-cli --session {session}"\n'
                  "implementer_session = 'session id: ([a-z-]+)'\n")
        if mode is not None:
            config += f'[loop]\nfix_session = "{mode}"\n'
        self.configure(config)
        calls = []
        reviewer = FakeAgent(REQUEST_CHANGES, APPROVE)

        def runner(cmd, cwd, timeout, **kwargs):
            if "ready" in cmd[-1]:
                return 0, "ready"
            calls.append((cmd, timeout, kwargs))
            if fail and cmd[0] == 'resume-cli':
                return 3, 'cannot open session'
            Commit(f'work-{len(calls)}').play(Path(cwd), len(calls))
            return 0, 'session id: original-session' if session else 'done'

        def dispatch(target, role, goal, cwd, **kwargs):
            if role != 'implement':
                if fallback:
                    agents.routes(target).commands['implement'] = 'fallback-cli'
                return reviewer(target, role, goal, cwd, **kwargs)
            return agents.agent(target, role, goal, cwd, **kwargs)

        real_cap = loop._check_run_cap
        checks = []

        def budget(*args):
            checks.append(True)
            if cap_retry and len(checks) == 3:
                with patch.object(loop, 'effective_work', return_value=100000000):
                    return real_cap(*args)
            return real_cap(*args)

        with patch.object(agents, 'run_capped', runner), \
                patch.object(loop, '_check_run_cap', budget):
            task = dict(a_task(), body=VALID_BODY)
            self.loop(fake=dispatch, provider=StubProvider(task))
        events = [json.loads(p) for (p,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'fix_session'")]
        return calls, events, checks

    def test_resume_uses_session_argv_and_findings_without_ticket(self):
        calls, events, _ = self.exercise_fix()
        self.assertEqual([c[0][0] for c in calls], ['primary-cli', 'resume-cli'])
        self.assertEqual(calls[1][0][:-1],
                         ['resume-cli', '--session', 'original-session'])
        prompt = calls[1][0][-1]
        self.assertIn('Reviewer findings:', prompt)
        self.assertIn('REQUEST_CHANGES', prompt)
        for instruction in ('ADDRESS', 'FOLLOW_UP', 'DECLINE'):
            self.assertIn(instruction, prompt)
        self.assertNotIn('## Summary', prompt)
        self.assertIn('## Summary', calls[0][0][-1])
        self.assertEqual(events, [{'arm': 'resume', 'resumed': True}])
        self.assertIn('on_start', calls[1][2])
        self.assertEqual(self.read('SELECT outcome FROM runs'), [('merged',)])

    def test_resume_skips_missing_session(self):
        calls, events, _ = self.exercise_fix(session=False)
        self.assertEqual([c[0][0] for c in calls], ['primary-cli', 'primary-cli'])
        self.assertIn('## Summary', calls[1][0][-1])
        self.assertEqual(events, [{'arm': 'resume', 'resumed': False,
                                  'reason': 'no recorded session'}])

    def test_resume_skips_active_fallback(self):
        calls, events, _ = self.exercise_fix(fallback=True)
        self.assertEqual([c[0][0] for c in calls], ['primary-cli', 'fallback-cli'])
        self.assertIn('## Summary', calls[1][0][-1])
        self.assertEqual(events, [{'arm': 'resume', 'resumed': False,
                                  'reason': 'fallback implementer route'}])

    def test_failed_resume_retries_fresh_once_with_budget(self):
        calls, events, checks = self.exercise_fix(fail=True)
        self.assertEqual([c[0][0] for c in calls],
                         ['primary-cli', 'resume-cli', 'primary-cli'])
        self.assertIn('## Summary', calls[2][0][-1])
        self.assertEqual(len(checks), 3)
        self.assertEqual(calls[1][1], calls[2][1])
        self.assertEqual(events, [{'arm': 'resume', 'resumed': False,
                                  'reason': 'resume exited 3'}])
        self.assertEqual(self.read('SELECT outcome FROM runs'), [('merged',)])

    def test_failed_resume_cannot_bypass_run_cap(self):
        calls, events, _ = self.exercise_fix(fail=True, cap_retry=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(events[0]['reason'], 'resume exited 3')
        self.assertEqual(self.read('SELECT outcome FROM runs'), [('failed',)])

    def test_default_keeps_fresh_turn_without_event(self):
        calls, events, _ = self.exercise_fix(mode=None)
        self.assertEqual([c[0][0] for c in calls], ['primary-cli', 'primary-cli'])
        self.assertIn('## Summary', calls[1][0][-1])
        self.assertEqual(events, [])

    def test_alternate_odd_and_even_runs(self):
        # Give the real claim API run numbers 7 and 8; no production state.
        import store
        original = store.claim
        for number, expected in ((7, 'resume-cli'), (8, 'primary-cli')):
            with self.subTest(run=number):
                def claim(*args, **kwargs):
                    for _ in range(number - 1):
                        prior = original(*args, **kwargs)
                        store.release(args[0], prior, 'failed', 'fixture history')
                    return original(*args, **kwargs)
                with patch.object(store, 'claim', claim):
                    calls, events, _ = self.exercise_fix(mode='alternate')
                self.assertEqual(calls[1][0][0], expected)
                self.assertEqual(events[-1]['arm'],
                                 'resume' if number == 7 else 'fresh')
                if number == 7:
                    self.tearDown()
                    self.doCleanups()
                    self.setUp()


class FixSessionConfigCases:
    def test_fix_session_startup_validation(self):
        import holophyte.config as config
        for setting, key in (("[agents]\nimplementer_resume = 'cli resume'",
                              'implementer_resume'),
                             ("[agents]\nimplementer_resume = 4", 'implementer_resume'),
                             ("[loop]\nfix_session = 'sometimes'", 'fix_session')):
            with self.subTest(setting=setting):
                self.locate(setting)
                with self.assertRaisesRegex(SystemExit, key):
                    config.check_document(self.tgt)
        self.locate('[agents]\nimplementer_resume = "cli resume {session}"\n'
                    '[loop]\nfix_session = "alternate"\n')
        config.check_document(self.tgt)
