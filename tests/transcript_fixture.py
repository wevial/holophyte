"""Recorded turns and transcript files for the run HTTP regressions."""
import json

import store
from tests.serve_fixture import ServeTestCase


class TranscriptCase(ServeTestCase):
    def turns(self):
        self.seed()
        with store.open(str(self.db)) as conn:
            store.record_event(conn, self.run, 'agent_turn', 'implement ended',
                               level="detail",
                               payload=json.dumps(dict(role='implement',
                                                       route='primary', seconds=10)))
            store.record_agent_session(conn, self.run, 'first', 'implement', 'primary')
            store.record_event(conn, self.run, 'agent_session', 'review session',
                               level="detail",
                               payload=json.dumps(dict(role='review', route='primary',
                                                       session_id='second')))
            for role, route, seconds in [('review', 'primary', 20),
                                         ('implement', 'fallback', 30)]:
                store.record_event(conn, self.run, 'agent_turn', 'ended',
                                   level="detail",
                                   payload=json.dumps(dict(role=role, route=route,
                                                           seconds=seconds)))
            store.record_agent_session(conn, self.run, 'third', 'implement', 'fallback')
        return f'/runs/{self.run}/turns'

    def transcript(self, root, text):
        root.mkdir(exist_ok=True)
        path = root / 'rollout-first.jsonl'
        path.write_text(json.dumps({'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': text}]}}) + '\n')
        return path
