"""Migration coverage for typed run failure reasons."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import store


class FailureKindMigrationTests(unittest.TestCase):
    def test_previous_schema_backfills_only_known_prefixes_and_preserves_reasons(self):
        cases = [
            ('verify failed: command 3 [false], exit 1; boom', 'verify'),
            ('reviewer returned no verdict line twice; candidate preserved',
             'review_route'),
            ('terminal adjudication: MALFORMED; no criterion detail', 'review_route'),
            ('terminal adjudication: FAIL; criterion 1 not met', 'unclassified'),
            ('fix round made no progress; 2 findings open', 'fix_no_progress'),
            ('implementer made no commits; discarded', 'no_commits'),
            ('implementer exceeded the 30 min budget; work kept', 'budget'),
            ('out of time: 90 min spent', 'budget'),
            ('merge lock /repo/lock held by run 3', 'merge_lock'),
            ('implementer transport failure (ECONNRESET)', 'infra'),
            ('swept by the supervisor in phase working: stale_heartbeat', 'swept'),
            ('something else mentions verify failed', 'unclassified'),
            (None, 'unclassified'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.db'
            conn = sqlite3.connect(path)
            # Frozen pre-change schema, not generated from the implementation.
            conn.executescript(Path(__file__).with_name('store_v28.sql').read_text())
            conn.execute("INSERT INTO projects (linearTeamId, repoPath,"
                         " defaultBranch, autonomyProfile)"
                         " VALUES ('t', '/repo', 'main', 'personal')")
            conn.execute("INSERT INTO tickets (projectId, linearIssueId,"
                         " linearIdentifier,"
                         " title, mirroredAt, status, affinity)"
                         " VALUES (1, 'i', 'KO-1', 'old', 1, 'ready', 'any')")
            for attempt, (reason, _) in enumerate(cases, 1):
                conn.execute("INSERT INTO runs (ticketId, projectId, attempt, phase,"
                             " startedAt, lastHeartbeat, endedAt, outcome,"
                             " outcomeReason)"
                             " VALUES (1, 1, ?, 'failed', 1, 2, 2, 'failed', ?)",
                             (attempt, reason))
            conn.execute('PRAGMA user_version = 28')
            conn.commit()
            conn.close()
            conn = store.open(path)
            self.addCleanup(conn.close)
            self.assertEqual(conn.execute(
                'SELECT outcomeReason, failureKind FROM runs ORDER BY attempt'
            ).fetchall(),
                cases)
            store.init(conn)
            self.assertEqual(conn.execute(
                'SELECT outcomeReason, failureKind FROM runs ORDER BY attempt'
            ).fetchall(),
                cases)


if __name__ == "__main__":
    unittest.main()
