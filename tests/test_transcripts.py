"""Transcript renderers keep known speech and commands, never raw records."""
import tempfile
import unittest
from pathlib import Path

from holophyte.transcripts import locate, render
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
