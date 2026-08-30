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
        # Store-owned too: the ticket's estimate as it stood at the claim, so
        # a finished run's estimate-vs-actual does not move when the ticket's
        # own `timeBoxMs` is later re-mirrored.
        "timeBoxMs",
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

# `runs` exactly as it shipped before `resumePhase` was added, kept verbatim
# rather than derived from store.SCHEMA: this is a real older store, and the
# point of the test below is that init() carries one forward. Creating it
# first and then calling init() is the upgrade as it actually happens —
# SCHEMA's `CREATE TABLE IF NOT EXISTS runs` leaves this table alone, so only
# the migration step can supply the missing column.
LEGACY_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY,
    ticketId          INTEGER NOT NULL REFERENCES tickets (id),
    projectId         INTEGER NOT NULL REFERENCES projects (id),
    attempt           INTEGER NOT NULL,
    phase             TEXT    NOT NULL
        CHECK (phase IN ('claimed', 'working', 'verifying', 'reviewing',
                         'addressing', 'merge_gate', 'awaiting_merge_approval',
                         'merging', 'squashing', 'done', 'blocked_on_operator',
                         'failed', 'killed')),
    workerId          TEXT,
    providerSessionId TEXT,
    branch            TEXT,
    prUrl             TEXT,
    startedAt         INTEGER NOT NULL,
    lastHeartbeat     INTEGER NOT NULL,
    endedAt           INTEGER,
    reviewRoundCount  INTEGER NOT NULL DEFAULT 0,
    outcome           TEXT
        CHECK (outcome IS NULL
               OR outcome IN ('merged', 'killed', 'abandoned', 'failed')),
    outcomeReason     TEXT,
    UNIQUE (ticketId, attempt)
);
"""


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


    def legacy_store(self):
        """A store whose `runs` predates `resumePhase`, then brought up to date."""
        conn = self.open()
        conn.executescript(LEGACY_RUNS_TABLE)
        store.init(conn)
        return conn

    def test_init_adds_columns_an_older_store_is_missing(self):
        conn = self.legacy_store()

        actual = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}

        self.assertEqual(actual, DOCUMENTED_COLUMNS["runs"])

    def test_a_migrated_column_carries_its_check_constraint(self):
        # ALTER TABLE ADD COLUMN keeps the CHECK, so the phase union is
        # enforced on an upgraded store as it is on a fresh one. Without this
        # the column would exist but accept anything, which is worse than the
        # missing column: it fails at read time instead of at write time.
        conn = self.legacy_store()
        run_id = self.a_run(conn)

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE runs SET resumePhase = 'nonsense' WHERE id = ?", (run_id,)
            )

    def test_resume_works_on_a_migrated_store(self):
        # The end-to-end version: before the migration this raised
        # `OperationalError: no such column: resumePhase` on every store that
        # existed before the column did.
        conn = self.legacy_store()
        run_id = self.a_run(conn, phase="blocked_on_operator")

        phase = store.resume(conn, run_id, guidance="use the other adapter")

        self.assertEqual(phase, "working")
        self.assertEqual(
            conn.execute("SELECT phase FROM runs WHERE id = ?", (run_id,)).fetchone(),
            ("working",),
        )

    def a_run(self, conn, phase="working"):
        """One project, ticket and run in `phase`, for the migration tests."""
        columns, values = A_PROJECT
        project_id = conn.execute(
            f"INSERT INTO projects ({columns}) VALUES (?, ?, ?, ?)", values
        ).lastrowid
        ticket_id = conn.execute(
            "INSERT INTO tickets"
            " (projectId, linearIssueId, linearIdentifier, title, status,"
            "  affinity, mirroredAt)"
            " VALUES (?, 'iss_1', 'HOL-1', 'a ticket', 'in_flight', 'any', 0)",
            (project_id,),
        ).lastrowid
        run_id = conn.execute(
            "INSERT INTO runs"
            " (ticketId, projectId, attempt, phase, startedAt, lastHeartbeat)"
            " VALUES (?, ?, 1, ?, 0, 0)",
            (ticket_id, project_id, phase),
        ).lastrowid
        conn.commit()
        return run_id


if __name__ == "__main__":
    unittest.main()
