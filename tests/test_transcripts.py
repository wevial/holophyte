"""Transcript renderers keep known speech and commands, never raw records."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.agents.fix_session
import holophyte.agents.roles
import holophyte.config.project
import holophyte.serve.serve_runs
import store
from holophyte.agents.transcripts import locate, render, turns
from tests.transcript_fixture import TranscriptCase

FIXTURES = Path(__file__).parent / 'fixtures' / 'transcripts'


class TranscriptTests(unittest.TestCase):
    def test_codex_rollout_in_order(self):
        self.assertEqual(render(FIXTURES / 'codex.jsonl'), [
            ('user', 'Check the project.'),
            ('assistant', 'I will run the checks.'),
            ('command', 'await tools.exec_command({cmd: "echo checked"});'),
            ('tool', 'checked\n\nExit code: 0'),
            ('assistant', 'All checks passed.'),
        ])

    def test_devin_export_in_order(self):
        self.assertEqual(render(FIXTURES / 'devin.json'), [
            ('user', 'Check the project.'),
            ('assistant', 'I will run the checks.'),
            ('command', 'echo checked'),
            ('tool', 'Output from command in shell 1:\nchecked\n\nExit code: 0'),
            ('assistant', 'All checks passed.'),
        ])

    def test_locate_rejects_traversal_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'allowed'
            root.mkdir()
            outside = Path(tmp) / 'rollout-session.jsonl'
            outside.write_text('{}')
            (root / outside.name).symlink_to(outside)
            self.assertIsNone(locate('codex', 'session', root))
            self.assertIsNone(locate('codex', '../session', root))
            inside = root / 'rollout-safe.jsonl'
            inside.write_text('{}')
            self.assertEqual(locate('codex', 'safe', root), inside)
            review = root / 'review-session'
            review.mkdir()
            export = review / 'export.json'
            export.write_text('{}')
            self.assertEqual(locate('devin', 'review-session', root), export)

    def test_function_calls_and_unknown_records(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rollout.jsonl'
            payloads = [
                {'type': 'function_call', 'name': 'exec_command', 'call_id': 'a',
                 'arguments': '{"cmd":"false"}'},
                {'type': 'function_call_output', 'call_id': 'a',
                 'output': 'Process exited with code 1\nOutput:\n'},
                {'type': 'function_call', 'name': 'unknown', 'call_id': 'b',
                 'arguments': 'DO NOT SHOW'},
                {'type': 'function_call_output', 'call_id': 'b',
                 'output': 'DO NOT SHOW'},
                {'type': 'future_type', 'text': 'DO NOT SHOW'},
                {'type': 'function_call', 'name': 'wait', 'call_id': 'c',
                 'arguments': '{"cell_id":"8"}'},
                {'type': 'function_call_output', 'call_id': 'c',
                 'output': '{"output":"done","exit_code":0}'},
            ]
            path.write_text('\n'.join(json.dumps({'type': 'response_item',
                                                  'payload': p}) for p in payloads)
                            + '\nnull\n{partial')
            self.assertEqual(render(path), [
                ('command', 'false'),
                ('tool', 'Process exited with code 1\nOutput:\n'),
                ('command', 'wait for cell 8'),
                ('tool', 'done\nExit code: 0'),
            ])


def session(role, route, session_id):
    return 'agent_session', dict(role=role, route=route, session_id=session_id)


def turn(role, route):
    return 'agent_turn', dict(role=role, route=route, seconds=1)


def paired(*events):
    return [(t['role'], t['route'], t['session_id']) for t in turns(
        (seq, kind, json.dumps(data)) for seq, (kind, data) in enumerate(events))]


class SessionPairingTests(unittest.TestCase):
    def test_implement_session_before_its_turn_pairs(self):
        self.assertEqual(paired(session('implement', 'primary', 'impl'),
                                turn('implement', 'primary')),
                         [('implement', 'primary', 'impl')])

    def test_implement_session_after_its_turn_pairs(self):
        self.assertEqual(paired(turn('implement', 'primary'),
                                session('implement', 'primary', 'impl')),
                         [('implement', 'primary', 'impl')])

    def test_fix_round_turn_resumes_the_implement_session(self):
        self.assertEqual(paired(session('implement', 'primary', 'impl'),
                                turn('implement', 'primary'),
                                session('review', 'primary', 'review-1'),
                                turn('review', 'primary'),
                                turn('implement', 'primary'),
                                session('review', 'primary', 'review-2'),
                                turn('review', 'primary')),
                         [('implement', 'primary', 'impl'),
                          ('review', 'primary', 'review-1'),
                          ('implement', 'primary', 'impl'),
                          ('review', 'primary', 'review-2')])

    def test_fresh_retry_session_does_not_relabel_the_resumed_turn(self):
        self.assertEqual(paired(session('implement', 'primary', 'first'),
                                turn('implement', 'primary'),
                                session('review', 'primary', 'review'),
                                turn('review', 'primary'),
                                turn('implement', 'primary'),
                                session('implement', 'primary', 'retry'),
                                turn('implement', 'primary')),
                         [('implement', 'primary', 'first'),
                          ('review', 'primary', 'review'),
                          ('implement', 'primary', 'first'),
                          ('implement', 'primary', 'retry')])

    def test_host_fix_turn_session_replaces_the_carried_one(self):
        self.assertEqual(paired(turn('implement', 'primary'),
                                session('implement', 'primary', 'first'),
                                turn('review', 'primary'),
                                turn('implement', 'primary'),
                                session('implement', 'primary', 'fix')),
                         [('implement', 'primary', 'first'),
                          ('review', 'primary', None),
                          ('implement', 'primary', 'fix')])

    def test_route_switching_to_post_turn_sessions_pairs_them_back(self):
        self.assertEqual(paired(session('implement', 'primary', 'container'),
                                turn('implement', 'primary'),
                                turn('implement', 'primary'),
                                session('implement', 'primary', 'host')),
                         [('implement', 'primary', 'container'),
                          ('implement', 'primary', 'host')])

    def test_each_route_keeps_its_own_implement_session(self):
        self.assertEqual(paired(session('implement', 'primary', 'impl-primary'),
                                turn('implement', 'primary'),
                                turn('implement', 'fallback'),
                                session('implement', 'fallback', 'impl-fallback'),
                                turn('implement', 'primary')),
                         [('implement', 'primary', 'impl-primary'),
                          ('implement', 'fallback', 'impl-fallback'),
                          ('implement', 'primary', 'impl-primary')])


class ContainerRunTurnsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
        holo = root / 'holo'
        holo.mkdir()
        (holo / 'config.toml').write_text(
            '[agents]\nimplementer_isolation = "container"\n'
            '[agents.implementer]\nharness = "claude"\nmodel = "sonnet"\n')
        self.target = holophyte.config.project.Project(
            path=repo, holo_dir=holo, store_path=holo / 'store.db',
            config_path=holo / 'config.toml', worktrees=root / 'repo.worktrees')
        self.conn = store.open(self.target.store_path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = store.ensure_project(self.conn, 'test', repo)
        ticket = store.mirror_ticket(self.conn, project, 'issue', 'KO-1', 'turns',
                                     acceptance_criteria=['shown'],
                                     verification_commands=['true'])
        self.run = store.claim(self.conn, project, ticket)

    def implement(self, argv=None):
        holophyte.agents.roles.agent(self.target, 'implement', 'goal',
                                     self.target.path, conn=self.conn,
                                     run_id=self.run, argv=argv)
        return self.conn.execute('SELECT providerSessionId FROM runs WHERE id = ?',
                                 (self.run,)).fetchone()[0]

    def test_resumed_and_retried_implement_turns_show_their_sessions(self):
        with patch.object(holophyte.agents.roles.launcher, 'launch',
                          lambda *args, **kwargs: (1, 'container turn')):
            first = self.implement()
            resume, _ = holophyte.agents.fix_session.resume_argv(
                self.target, self.conn, self.run)
            self.implement(resume)
            retry = self.implement()
        self.assertNotEqual(first, retry)
        code, body = holophyte.serve.serve_runs.run_turns(self.target, str(self.run))
        self.assertEqual(code, 200)
        self.assertEqual([(t['role'], t['session_id']) for t in body['turns']],
                         [('implement', first), ('implement', first),
                          ('implement', retry)])


class TranscriptFallbackTests(TranscriptCase):
    def test_transcript_render_failure_falls_back_to_later_root(self):
        path = self.turns()
        stale, valid = self.root / 'stale', self.root / 'valid'
        self.transcript(stale, '').write_bytes(b'\xff\n')
        self.transcript(valid, 'Recovered transcript')
        self.start(f'[serve]\ntranscripts = ["{stale}", "{valid}"]\n')
        turn = self.request('GET', path)[2]['turns'][0]
        url = f"{path}/{turn['id']}/transcript"
        code, _, body = self.request('GET', url)
        self.assertEqual(code, 200)
        self.assertEqual(body, {'entries': [
            {'speaker': 'assistant', 'text': 'Recovered transcript'}]})
        (valid / 'rollout-first.jsonl').unlink()
        self.assertEqual(self.request('GET', url)[0], 404)
