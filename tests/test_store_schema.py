"""Schema bootstrap contract for the v2 store.

The expected table and column names below are transcribed from
docs/v2/state-model.md §1-§2 (plus the §7 lease column) by hand, on purpose:
reading them back out of store.SCHEMA would only prove the module agrees with
itself. This transcription is the independent oracle.

Run: python3 -m unittest discover -s tests -p 'test_store*' -v
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import store

# table -> the fields the state model documents for it.
DOCUMENTED_COLUMNS = {
    "projects": {
        "id", "linearTeamId", "repoPath", "defaultBranch", "autonomyProfile",
        "highRiskPaths", "verificationDefault", "activeRunId",
    },
    "tickets": {
        "id", "projectId", "linearIssueId", "linearIdentifier", "title",
        "status", "acceptanceCriteria", "verificationCommands", "timeBoxMs",
        "affinity", "dependsOn", "activeRunId", "lastRunId", "blockedQuestion",
        "splitDepth", "mirroredAt",
    },
    "runs": {
        "id", "ticketId", "projectId", "attempt", "phase", "workerId",
        "providerSessionId", "branch", "prUrl", "startedAt", "lastHeartbeat",
        "endedAt", "reviewRoundCount", "outcome", "outcomeReason",
        # Store-owned, not a documented field: §5 requires a resume to
        # "re-enter the phase it left" and leaves the mechanism to us, so
        # `resume()` reads the parked phase from this column.
        "resumePhase",
    },
    "reviewRounds": {
        "id", "runId", "round", "verificationResults", "verdict", "findings",
        "findingsFingerprint", "reviewerModel", "startedAt", "endedAt",
    },
    "runEvents": {
        "id", "runId", "seq", "level", "kind", "summary", "payload", "at",
    },
    "interventions": {
        "id", "runId", "source", "trigger", "action", "question", "guidance",
        "at",
    },
    "linearDeliveries": {"deliveryId", "processedAt"},
}

A_PROJECT = (
    "linearTeamId, repoPath, defaultBranch, autonomyProfile",
    ("team_abc", "/srv/dev/holophyte", "main", "personal"),
)


class StoreSchemaTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"

    def open(self):
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        return conn

    def test_init_creates_the_seven_documented_tables_and_columns(self):
        conn = self.open()

        store.init(conn)

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        self.assertEqual(tables, set(DOCUMENTED_COLUMNS))
        for table, expected in DOCUMENTED_COLUMNS.items():
            with self.subTest(table=table):
                actual = {
                    row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')
                }
                self.assertEqual(actual, expected)

    def test_init_again_is_harmless_and_keeps_existing_rows(self):
        conn = self.open()
        store.init(conn)
        columns, values = A_PROJECT
        conn.execute(
            f"INSERT INTO projects ({columns}) VALUES (?, ?, ?, ?)", values
        )
        conn.commit()

        store.init(conn)

        self.assertEqual(
            conn.execute(
                f"SELECT {columns} FROM projects"
            ).fetchall(),
            [values],
        )

    def test_open_puts_the_database_in_wal_mode(self):
        conn = self.open()

        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

        self.assertEqual(mode.lower(), "wal")

    def test_unknown_enum_value_is_rejected_by_the_schema(self):
        # The union types in the state model are CHECK constraints, so a bad
        # status is a database error rather than a caller's oversight.
        conn = self.open()
        store.init(conn)
        columns, values = A_PROJECT

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO projects ({columns}) VALUES (?, ?, 'main', 'nonsense')",
                values[:2],
            )


if __name__ == "__main__":
    unittest.main()
