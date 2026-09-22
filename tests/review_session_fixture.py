"""Reviewer session contract exercised through the real factory loop."""
import json
import os
from pathlib import Path
from unittest.mock import patch

from fake_agent import APPROVE, MALFORMED, REQUEST_CHANGES, Commit, FakeAgent
from loop_fixture import VALID_BODY, StubProvider, a_task

import holophyte.agents as agents


class ReviewSessionCases:
    def exercise_review_session(self, mode='resume', session=True, fallback=False,
                                number=1, retry=False):
        import store
        config = '[agents]\nreviewer = "review-cli"\n'
        if mode is not None:
            config += f'[loop]\nreview_session = "{mode}"\n'
        self.configure(config)
        calls = []
        implementer = FakeAgent(Commit('work'), Commit('fix'))
        original = store.claim

        def claim(*args, **kwargs):
            for _ in range(number - 1):
                prior = original(*args, **kwargs)
                store.release(args[0], prior, 'failed', 'fixture history')
            return original(*args, **kwargs)

        replies = ([MALFORMED] if retry else []) + [REQUEST_CHANGES, APPROVE]

        def runner(cmd, cwd, timeout, **kwargs):
            env = kwargs['env']
            calls.append({k: v for k, v in env.items()
                          if k.startswith('HOLOPHYTE_REVIEW_')})
            if session:
                (Path(env['HOLOPHYTE_REVIEW_SCRATCH']) / 'session').write_text(
                    'first-session' if len(calls) == 1 else 'later-session')
            return 0, replies[len(calls) - 1].text

        def dispatch(target, role, goal, cwd, **kwargs):
            if role == 'implement':
                return implementer(target, role, goal, cwd, **kwargs)
            if fallback and calls:
                agents.routes(target).commands['review'] = 'fallback-cli'
            return agents.agent(target, role, goal, cwd, **kwargs)

        with patch.object(agents, 'run_capped', runner), \
                patch.object(store, 'claim', claim), \
                patch.dict(os.environ, HOLOPHYTE_REVIEW_RESUME='inherited'):
            task = dict(a_task(), body=VALID_BODY)
            self.loop(fake=dispatch, provider=StubProvider(task))
        events = [json.loads(p) for (p,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'review_session'")]
        self.assertEqual(len(calls), len(replies))
        self.assertNotIn('HOLOPHYTE_REVIEW_RESUME', calls[0])
        self.assertEqual(self.read('SELECT outcome FROM runs ORDER BY id DESC LIMIT 1'),
                         [('merged',)])
        return calls, events

    def test_review_resume_and_safe_fresh_arms(self):
        cases = [({}, 'first-session', {'arm': 'resume', 'requested': True}),
                 ({'session': False}, None, {'arm': 'resume', 'requested': False,
                                            'reason': 'no recorded session'}),
                 ({'fallback': True}, None, {'arm': 'resume', 'requested': False,
                                            'reason': 'fallback reviewer route'}),
                 ({'mode': 'alternate', 'number': 7}, 'first-session',
                  {'arm': 'resume', 'requested': True}),
                 ({'mode': 'alternate', 'number': 8}, None,
                  {'arm': 'fresh', 'requested': False, 'reason': 'fresh arm'}),
                 ({'mode': None}, None, None)]
        for index, (kwargs, expected, event) in enumerate(cases):
            with self.subTest(case=kwargs):
                if index:
                    self.tearDown()
                    self.doCleanups()
                    self.setUp()
                calls, events = self.exercise_review_session(**kwargs)
                self.assertEqual(calls[1].get('HOLOPHYTE_REVIEW_RESUME'), expected)
                self.assertEqual(events, [event] if event else [])

    def test_review_resume_uses_session_from_round_one_retry(self):
        calls, events = self.exercise_review_session(retry=True)
        self.assertNotIn('HOLOPHYTE_REVIEW_RESUME', calls[1])
        self.assertEqual(calls[2].get('HOLOPHYTE_REVIEW_RESUME'), 'later-session')
        self.assertEqual(events, [{'arm': 'resume', 'requested': True}])
        sessions = [json.loads(p) for (p,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'agent_session' ORDER BY seq")]
        self.assertEqual([(e['session_id'], e['round']) for e in sessions],
                         [('first-session', 1), ('later-session', 1),
                          ('later-session', 2)])
