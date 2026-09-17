"""Active project outages are separate from bounded ledger history."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from serve_fixture import ServeTestCase  # noqa: E402

import store  # noqa: E402
from store import launch_backoff  # noqa: E402


class ActiveOutageTests(ServeTestCase):
    def test_full_history_and_later_windows_preserve_active_outages(self):
        self.seed()
        conn = store.open(str(self.db))
        try:
            project = conn.execute(
                'SELECT projectId FROM runs WHERE id=?', (self.run,)).fetchone()[0]
            launch_backoff.failure(conn, project, 'quota exhausted', self.now,
                                   run_id=self.run)
            store.record_intervention(conn, self.run, 'resume', 'historical row',
                                      now=self.now + 1)
        finally:
            conn.close()
        self.start()
        code, _, page = self.request(
            'GET', '/ledger?since=0&kind=intervention&limit=1')
        self.assertEqual(code, 200)
        self.assertEqual(len(page['entries']), 1)
        outage, = page['active_outages']
        self.assertEqual(outage['project'], project)
        self.assertEqual(outage['at'], self.now)
        self.assertEqual(outage['reason'], 'quota exhausted')
        _, _, later = self.request('GET', f'/ledger?since={self.now + 10}')
        self.assertEqual(later['active_outages'], [outage])
        for query in ('ticket=KO-1', 'kind=merge'):
            _, _, filtered = self.request('GET', f'/ledger?since=0&{query}')
            self.assertEqual(filtered['active_outages'], [])
