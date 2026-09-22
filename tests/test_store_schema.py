"""Schema bootstrap contract for the v2 store."""
from __future__ import annotations

import getpass
import json
import os
import socket
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import store
import store.schema
import store.tickets
from tests.schema_fixture import DOCUMENTED_COLUMNS
from tests.ticket_url_fixture import assert_schema_url

A_PROJECT = (
    "linearTeamId, repoPath, defaultBranch, autonomyProfile",
    ("team_abc", "/repos/holophyte", "main", "personal"),
)

# `supervisorHeartbeats` as it shipped before `host` was added, for the same
# reason as `LEGACY_RUNS_TABLE` below: init() must carry it forward.
LEGACY_HEARTBEATS_TABLE = """
CREATE TABLE IF NOT EXISTS supervisorHeartbeats (
    pid       INTEGER NOT NULL,
    startedAt INTEGER NOT NULL,
    lastBeat  INTEGER NOT NULL,
    passes    INTEGER NOT NULL,
    PRIMARY KEY (pid, startedAt)
);
"""

# `runs` exactly as it shipped before `resumePhase` was added, kept verbatim
# rather than derived from store.schema.SCHEMA: this is a real older store, and the
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


class TicketUrlMigrationTests(unittest.TestCase):
    def test_version_22_adds_nullable_url_to_existing_ticket(self):
        assert_schema_url(self)


class BoardStateMigrationTests(unittest.TestCase):
    def test_version_24_adds_nullable_board_state_to_existing_ticket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            conn = sqlite3.connect(path)
            previous = "\n".join(
                line for line in store.schema.SCHEMA.splitlines()
                if not line.strip().startswith("boardState "))
            conn.executescript(previous)
            project = store.tickets.ensure_project(conn, "team", "/repo")
            conn.execute("INSERT INTO tickets (projectId, linearIssueId,"
                         " linearIdentifier, title, status, mirroredAt, affinity)"
                         " VALUES (?, 'issue', 'KO-1', 'old', 'ready', 1, 'any')",
                         (project,))
            conn.execute("PRAGMA user_version = 24")
            conn.commit()
            conn.close()
            conn = store.open(path)
            try:
                self.assertEqual(conn.execute(
                    "SELECT boardState FROM tickets").fetchone(), (None,))
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone(),
                                 (store.schema.SCHEMA_VERSION,))
            finally:
                conn.close()


class ApprovalStampMigrationTests(unittest.TestCase):
    def test_version_25_adds_nullable_approval_stamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            conn = sqlite3.connect(path)
            previous = "\n".join(
                line for line in store.schema.SCHEMA.splitlines()
                if not line.strip().startswith(("approvedAt ", "approvedBy ")))
            conn.executescript(previous)
            project = store.tickets.ensure_project(conn, "team", "/repo")
            ticket = store.tickets.mirror_ticket(
                conn, project, linear_issue_id="issue", linear_identifier="KO-1",
                title="old candidate")
            store.claim(conn, project, ticket, now=1)
            conn.execute("PRAGMA user_version = 25")
            conn.commit()
            conn.close()
            conn = store.open(path)
            try:
                self.assertEqual(conn.execute(
                    "SELECT approvedAt, approvedBy FROM runs").fetchall(),
                    [(None, None)])
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone(),
                                 (store.schema.SCHEMA_VERSION,))
            finally:
                conn.close()


class StoreSchemaTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"

    def open(self):
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        return conn

    def raw(self):
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        return conn

    def test_non_migrating_open_refuses_older_without_changing_version(self):
        self.open().close()
        older = store.SCHEMA_VERSION - 1
        self.raw().execute(f"PRAGMA user_version = {older}")
        with self.assertRaises(store.SchemaOlder) as caught:
            store.open(self.path, migrate=False)
        self.assertEqual(
            str(caught.exception),
            f"store at {self.path} is schema {older}; this build expects "
            f"{store.SCHEMA_VERSION}; start the loop or the serve daemon to "
            "migrate it, or run the command from the build that wrote it")
        self.assertEqual(self.raw().execute("PRAGMA user_version").fetchone(),
                         (older,))

    def test_non_migrating_open_refuses_newer_identically(self):
        newer = store.SCHEMA_VERSION + 1
        self.raw().execute(f"PRAGMA user_version = {newer}")
        before = self.path.read_bytes()
        messages = []
        for migrate in (True, False):
            with self.assertRaises(store.SchemaNewer) as caught:
                store.open(self.path, migrate=migrate)
            messages.append(str(caught.exception))
        self.assertEqual(messages[0], messages[1])
        self.assertEqual(self.path.read_bytes(), before)

    def test_non_migrating_open_does_not_recreate_missing_indexes(self):
        conn = self.open()
        conn.execute("DROP INDEX runs_ticketId")
        conn.close()
        conn = store.open(self.path, migrate=False)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'runs_ticketId'"
        ).fetchall(), [])
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone(), (1,))
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone(), ("wal",))

    def test_init_creates_the_documented_tables_and_columns(self):
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
        # Built on a raw connection before store.open() sees the file: an
        # older store exists before the module that carries it forward does.
        self.raw().executescript(LEGACY_RUNS_TABLE)
        conn = self.open()
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

    def test_a_migrated_outcome_class_defaults_to_work_and_is_checked(self):
        # A run that ended before the column existed is a `work` failure —
        # what every failure was until then — so an upgraded store escalates
        # exactly as it did. And the CHECK travels with the ALTER, so a class
        # nobody defined is refused at write time on an old store too.
        conn = self.open()
        conn.executescript(LEGACY_RUNS_TABLE)
        # The legacy fixture is the `runs` table alone; the row's parent
        # tables arrive with init(), so the foreign keys are off while the
        # old row is written the way the old module wrote it.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO runs (id, ticketId, projectId, attempt, phase,"
            " startedAt, lastHeartbeat, endedAt, outcome)"
            " VALUES (1, 1, 1, 1, 'failed', 0, 0, 1, 'failed')")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")

        store.init(conn)

        self.assertEqual(
            conn.execute("SELECT outcomeClass FROM runs").fetchall(),
            [("work",)])
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE runs SET outcomeClass = 'nonsense'")

    def test_a_claim_records_the_hostname_that_made_it(self):
        # The federation model pins a target to one host and lets a store be
        # read elsewhere; the row itself is the only place "where is this run
        # executing" can be answered from.
        conn = self.open()
        store.init(conn)
        project = store.tickets.ensure_project(conn, "team-h", "/repos/x")
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="iss-h", linear_identifier="KO-9",
            title="a ticket", acceptance_criteria=["Given, then"],
            verification_commands=["echo ok"], time_box_ms=None)
        store.tickets.transition(conn, ticket, "in_flight")

        run_id = store.claim(conn, project, ticket, now=1_000)

        self.assertEqual(
            conn.execute("SELECT host FROM runs WHERE id = ?",
                         (run_id,)).fetchone(),
            (socket.gethostname(),))

    def test_a_supervisor_heartbeat_records_its_host(self):
        conn = self.open()
        store.init(conn)

        store.record_supervisor_heartbeat(conn, pid=4242, started_at=1, now=2)
        store.record_supervisor_heartbeat(conn, pid=4242, started_at=1, now=3)

        self.assertEqual(
            conn.execute(
                "SELECT host, passes FROM supervisorHeartbeats").fetchall(),
            [(socket.gethostname(), 2)])
        self.assertEqual(store.latest_supervisor_heartbeat(conn)[-1],
                         socket.gethostname())

    def test_init_adds_host_to_an_older_heartbeats_table_as_nullable(self):
        # A beat an older supervisor wrote has no host and keeps none; the
        # next beat lands with one. Nothing here is backfilled.
        raw = self.raw()
        raw.executescript(LEGACY_HEARTBEATS_TABLE)
        raw.execute(
            "INSERT INTO supervisorHeartbeats (pid, startedAt, lastBeat, passes)"
            " VALUES (1, 1, 1, 1)")
        raw.commit()
        raw.close()

        conn = self.open()
        store.init(conn)
        store.record_supervisor_heartbeat(conn, pid=2, started_at=2, now=3)

        self.assertEqual(
            {row[1] for row in conn.execute(
                "PRAGMA table_info(supervisorHeartbeats)")},
            DOCUMENTED_COLUMNS["supervisorHeartbeats"])
        self.assertEqual(
            conn.execute("SELECT pid, host FROM supervisorHeartbeats"
                         " ORDER BY pid").fetchall(),
            [(1, None), (2, socket.gethostname())])

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

    def test_init_backfills_review_round_counts_an_older_store_never_wrote(self):
        # `reviewRoundCount` has been in the schema since the first version,
        # but nothing wrote it until close-out began stamping it -- so a run
        # that ended before then holds the column's DEFAULT 0 next to the
        # review rounds it actually took. The report and FINDINGS.md read the
        # column, and a stale 0 there is a wrong answer rather than a missing
        # one.
        conn = self.legacy_store()
        ended = self.a_run(conn, phase="done", ended_at=5_000)
        self.a_review_round(conn, ended, round=1)
        self.a_review_round(conn, ended, round=2)

        store.init(conn)

        self.assertEqual(self.review_round_count(conn, ended), 2)

    def test_backfill_leaves_a_run_still_in_flight_alone(self):
        # An unfinished run has not reached the close-out that owns this
        # column, so its count is not final and the backfill must not
        # pre-empt it -- the rounds it is about to file would make whatever
        # was written here wrong again.
        conn = self.legacy_store()
        running = self.a_run(conn, phase="reviewing")
        self.a_review_round(conn, running, round=1)

        store.init(conn)

        self.assertEqual(self.review_round_count(conn, running), 0)

    def test_backfill_is_idempotent_and_does_not_undo_a_stamped_count(self):
        # The second call has nothing left to repair, and a count already
        # stamped by `release()` agrees with the rows it was counted from, so
        # it survives untouched.
        conn = self.legacy_store()
        ended = self.a_run(conn, phase="done", ended_at=5_000)
        self.a_review_round(conn, ended, round=1)

        store.init(conn)
        store.init(conn)

        self.assertEqual(self.review_round_count(conn, ended), 1)

    def test_reopening_excludes_error_rounds_from_ended_run_counts(self):
        conn = self.legacy_store()
        ended = self.a_run(conn, phase="done", ended_at=5_000)
        self.a_review_round(conn, ended, round=1)
        self.a_review_round(conn, ended, round=2)
        self.a_review_round(conn, ended, round=3, verdict="error")
        # Exercise an unstamped legacy count, a correct close-out count,
        # and a count inflated by the old startup backfill.
        for previous_count in (0, 2, 3):
            with self.subTest(previous_count=previous_count):
                conn.execute("UPDATE runs SET reviewRoundCount = ? WHERE id = ?",
                             (previous_count, ended))
                conn.commit()
                conn.close()
                conn = self.open()
                store.init(conn)
                self.assertEqual(self.review_round_count(conn, ended), 2)

    def review_round_count(self, conn, run_id):
        (count,) = conn.execute(
            "SELECT reviewRoundCount FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return count

    def a_review_round(self, conn, run_id, round, verdict="pass"):  # noqa: A002 - the column's name
        """One `reviewRounds` row on `run_id`, for the backfill tests."""
        conn.execute(
            "INSERT INTO reviewRounds"
            " (runId, round, verdict, findingsFingerprint, reviewerModel,"
            "  startedAt)"
            " VALUES (?, ?, ?, 'fp', 'a-model', 0)",
            (run_id, round, verdict),
        )
        conn.commit()

    def a_run(self, conn, phase="working", ended_at=None):
        """One project, ticket and run in `phase`, for the migration tests."""
        columns, values = A_PROJECT
        nth = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] + 1
        project_id = conn.execute(
            f"INSERT INTO projects ({columns}) VALUES (?, ?, ?, ?)",
            (f"{values[0]}_{nth}",) + values[1:],
        ).lastrowid
        ticket_id = conn.execute(
            "INSERT INTO tickets"
            " (projectId, linearIssueId, linearIdentifier, title, status,"
            "  affinity, mirroredAt)"
            " VALUES (?, ?, ?, 'a ticket', 'in_flight', 'any', 0)",
            (project_id, f"iss_{nth}", f"HOL-{nth}"),
        ).lastrowid
        run_id = conn.execute(
            "INSERT INTO runs"
            " (ticketId, projectId, attempt, phase, startedAt, lastHeartbeat,"
            "  endedAt)"
            " VALUES (?, ?, 1, ?, 0, 0, ?)",
            (ticket_id, project_id, phase, ended_at),
        ).lastrowid
        conn.commit()
        return run_id


# The three indexes the ticket names, by the column each one covers. Named
# here by hand rather than read from store.schema.INDEXES: the point is that these
# foreign keys are indexed, whatever the module chooses to call the indexes.
HOT_FOREIGN_KEYS = {
    ("runs", "ticketId"),
    ("reviewRounds", "runId"),
    ("runEvents", "runId"),
}


class StoreSchemaVersionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"

    def test_migration_records_process_once_and_reopen_is_quiet(self):
        conn = store.open(self.path)
        conn.execute("DELETE FROM interventions WHERE action = 'migrate'")
        conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION - 1}")
        conn.commit()
        conn.close()
        before = int(time.time() * 1000)
        conn = store.open(self.path)
        committed = int(time.time() * 1000)
        rows = conn.execute(
            "SELECT runId, source, note, at FROM interventions"
            " WHERE action = 'migrate'").fetchall()
        self.assertEqual(len(rows), 1)
        run, source, note, at = rows[0]
        detail = json.loads(note)
        self.assertIsNone(run)
        self.assertEqual(source, "factory")
        self.assertEqual(detail["from"], store.SCHEMA_VERSION - 1)
        self.assertEqual(detail["to"], store.SCHEMA_VERSION)
        self.assertTrue(detail["build"])
        self.assertEqual(detail["pid"], os.getpid())
        self.assertEqual(detail["ppid"], os.getppid())
        self.assertEqual(detail["argv"], sys.argv)
        self.assertEqual(detail["user"], getpass.getuser())
        self.assertEqual(detail["at"], at)
        self.assertLessEqual(before, at)
        self.assertLessEqual(at, committed)
        conn.close()
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        store.init(conn)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM interventions WHERE action = 'migrate'"
        ).fetchone()[0], 1)

    def test_migration_evidence_rolls_back_with_failed_stamp_and_without_git(self):
        conn = store.open(self.path)
        conn.execute("DELETE FROM interventions WHERE action = 'migrate'")
        conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION - 1}")
        conn.commit()
        self.addCleanup(conn.close)
        # Refuse only the version write, after the audit INSERT has run.
        def deny_stamp(action, name, value, database, context):
            if action == sqlite3.SQLITE_PRAGMA and name == "user_version" and value:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny_stamp)
        with patch("store.schema.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(sqlite3.DatabaseError):
                store.init(conn)
            conn.set_authorizer(None)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                             store.SCHEMA_VERSION - 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM interventions WHERE action = 'migrate'"
            ).fetchone()[0], 0)
            store.init(conn)
        note = conn.execute(
            "SELECT note FROM interventions WHERE action = 'migrate'"
        ).fetchone()[0]
        self.assertEqual(json.loads(note)["build"], "unknown")

    def test_version_16_accepts_rejection_after_migration(self):
        raw = self.raw()
        old = store.schema.SCHEMA.replace(", 'rejected'", "")
        raw.executescript(old)
        raw.execute("PRAGMA user_version = 16")
        raw.commit()
        project = store.tickets.ensure_project(raw, "team", "/repos/project")
        ticket = store.tickets.mirror_ticket(
            raw, project, linear_issue_id="issue", linear_identifier="KO-1",
            title="candidate")
        run = store.claim(raw, project, ticket)
        store.record_event(raw, run, "candidate", "preserve this history")
        raw.close()
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         store.SCHEMA_VERSION)
        store.set_phase(conn, run, "merge_gate")
        store.release(conn, run, "rejected", "closed by alice")
        self.assertEqual(conn.execute("SELECT phase, outcome FROM runs")
                         .fetchone(), ("rejected", "rejected"))
        self.assertEqual(conn.execute("SELECT summary FROM runEvents"
                                      " WHERE kind = 'candidate'").fetchone(),
                         ("preserve this history",))
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def raw(self):
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        return conn

    def current_unstamped_store(self):
        """A store with every current column and `user_version` still 0."""
        conn = store.open(self.path)
        store.init(conn)
        conn.execute(f"INSERT INTO projects ({A_PROJECT[0]}) VALUES (?, ?, ?, ?)",
                     A_PROJECT[1])
        conn.commit()
        conn.close()
        raw = self.raw()
        raw.execute("PRAGMA user_version = 0")
        raw.commit()
        rows = raw.execute("SELECT * FROM projects").fetchall()
        raw.close()
        self.assertEqual(self.user_version(), 0)
        return rows

    def user_version(self):
        return self.raw().execute("PRAGMA user_version").fetchone()[0]

    def test_a_version_zero_store_is_stamped_on_open_without_data_changes(self):
        before = self.current_unstamped_store()

        conn = store.open(self.path)
        self.addCleanup(conn.close)

        self.assertEqual(self.user_version(), store.schema.SCHEMA_VERSION)
        self.assertEqual(conn.execute("SELECT * FROM projects").fetchall(),
                         before)

    def test_a_version_3_store_gains_merge_sha_and_keeps_its_rows(self):
        """A store stamped 3 has no `runs.mergeSha`; opening it with this
        build adds the column, leaves the run it held with a null there,
        and stamps version 4."""
        conn = store.open(self.path)
        store.init(conn)
        project = store.tickets.ensure_project(conn, "team-1", "/repos/holophyte")
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-1", linear_identifier="KO-1",
            title="ticket 1")
        run_id = store.claim(conn, project, ticket, now=1_700_000_000_000)
        for phase in ("merge_gate", "merging"):
            store.set_phase(conn, run_id, phase, now=1_700_000_050_000)
        store.release(conn, run_id, "merged", now=1_700_000_060_000)
        conn.close()
        raw = self.raw()
        raw.execute("ALTER TABLE runs DROP COLUMN mergeSha")
        raw.execute("PRAGMA user_version = 3")
        raw.commit()
        columns = {row[1] for row in raw.execute("PRAGMA table_info(runs)")}
        raw.close()
        self.assertNotIn("mergeSha", columns)
        self.assertEqual(self.user_version(), 3)

        conn = store.open(self.path)
        self.addCleanup(conn.close)

        self.assertGreaterEqual(store.schema.SCHEMA_VERSION, 4)
        self.assertEqual(self.user_version(), store.schema.SCHEMA_VERSION)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        self.assertIn("mergeSha", columns)
        self.assertEqual(
            conn.execute("SELECT id, outcome, mergeSha FROM runs").fetchall(),
            [(run_id, "merged", None)])

    def test_a_version_4_store_migrates_in_place_and_still_reports(self):
        """Migrate version 4 while retaining its runs and intervention history."""
        conn = store.open(self.path)
        store.init(conn)
        project = store.tickets.ensure_project(conn, "team-1", "/repos/holophyte")
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-1", linear_identifier="KO-1",
            title="ticket 1")
        run_id = store.claim(conn, project, ticket, now=1_700_000_000_000)
        store.release(conn, run_id, "failed", "verify", now=1_700_000_060_000)
        conn.execute("DROP TABLE interventions")
        conn.executescript(VERSION_4_INTERVENTIONS_TABLE)
        conn.execute(
            'INSERT INTO interventions (runId, source, "trigger", "action", at)'
            " VALUES (?, 'human', 'manual', 'requeue', ?)",
            (run_id, 1_700_000_120_000))
        conn.execute("DROP TABLE ledger")
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        conn.close()
        self.assertEqual(self.user_version(), 4)
        raw = sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute(
                'INSERT INTO interventions (runId, source, "trigger",'
                ' "action", at) VALUES (?, \'human\', \'manual\','
                ' \'approve\', 1)', (run_id,))
        with self.assertRaises(sqlite3.OperationalError):
            raw.execute("SELECT 1 FROM ledger")
        raw.close()

        conn = store.open(self.path)
        self.addCleanup(conn.close)

        self.assertGreaterEqual(store.schema.SCHEMA_VERSION, 6)
        self.assertEqual(self.user_version(), store.schema.SCHEMA_VERSION)
        self.assertEqual(
            conn.execute('SELECT runId, "action" FROM interventions'
                         " WHERE action != 'migrate'"
                         " ORDER BY id").fetchall(),
            [(run_id, "requeue")])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ledger")
                         .fetchone(), (0,))
        store.record_intervention(conn, run_id, "approve", "ok")
        self.assertEqual(
            conn.execute('SELECT "action" FROM interventions'
                         " WHERE action != 'migrate' ORDER BY id")
            .fetchall(), [("requeue",), ("approve",)])
        self.assertEqual(
            conn.execute("SELECT runId, ticketId, kind, source FROM ledger")
            .fetchall(), [(run_id, ticket, "intervention", "operator")])
        import holophyte.report
        table = "\n".join(holophyte.report.report_lines(conn))
        self.assertIn("KO-1", table)
        self.assertIn("failed", table)

    def test_a_store_stamped_newer_is_refused_and_untouched(self):
        self.current_unstamped_store()
        newer = store.schema.SCHEMA_VERSION + 1
        raw = self.raw()
        raw.execute(f"PRAGMA user_version = {newer}")
        raw.commit()
        raw.close()
        before = self.path.read_bytes()

        with self.assertRaises(SystemExit) as caught:
            store.open(self.path)

        message = str(caught.exception)
        self.assertIn(str(newer), message)
        self.assertIn(str(store.schema.SCHEMA_VERSION), message)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.user_version(), newer)

    def test_the_hot_foreign_keys_are_indexed_after_open(self):
        # The UNIQUE constraints already give every hot column an automatic
        # index, so asserting "some index covers the column" would pass
        # without the ticket's DDL. Assert the three named indexes exist and
        # each leads on its foreign key.
        conn = store.open(self.path)
        self.addCleanup(conn.close)

        named = {}
        for (name, table) in conn.execute(
                "SELECT name, tbl_name FROM sqlite_master"
                " WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex%'"
                ).fetchall():
            columns = [row[2] for row in
                       conn.execute(f"PRAGMA index_info({name})")]
            named[name] = (table, columns[0])

        expected = {f"{table}_{column}": (table, column)
                    for (table, column) in HOT_FOREIGN_KEYS}
        self.assertEqual({k: named.get(k) for k in expected}, expected)


# `interventions` exactly as schema version 4 shipped it: 'requeue' in the
# action CHECK, 'approve' not yet. Kept verbatim so the migration test is
# that a real version-4 store is carried to 5 with its rows intact.
VERSION_4_INTERVENTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS interventions (
    id        INTEGER PRIMARY KEY,
    runId     INTEGER NOT NULL REFERENCES runs (id),
    source    TEXT    NOT NULL CHECK (source IN ('supervisor', 'human')),
    "trigger" TEXT    NOT NULL
        CHECK ("trigger" IN ('time_box', 'off_criteria', 'looping',
                             'review_stuck', 'linear_cancelled', 'manual')),
    "action"  TEXT    NOT NULL
        CHECK ("action" IN ('redirect', 'kill', 'extend_time_box', 'resume',
                            'close_out', 'requeue')),
    question  TEXT,
    guidance  TEXT,
    at        INTEGER NOT NULL
);
"""

# `interventions` exactly as schema version 6 shipped it: 'approve' in the
# action CHECK, 'repoint' not yet. The migration test is that a real
# version-6 store is carried to 7 with its rows intact.
VERSION_6_INTERVENTIONS_TABLE = VERSION_4_INTERVENTIONS_TABLE.replace(
    "'requeue'))", "'requeue', 'approve'))")


class Version6MigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"

    def user_version(self):
        raw = sqlite3.connect(self.path)
        try:
            return raw.execute("PRAGMA user_version").fetchone()[0]
        finally:
            raw.close()

    def test_a_version_6_store_is_rebuilt_to_accept_repoint(self):
        """A store stamped 6 refuses a 'repoint' row; opening it with this
        build rebuilds the table in place, keeps the 'approve' row it held,
        stamps the current version, and a repoint row then lands."""
        conn = store.open(self.path)
        project = store.tickets.ensure_project(conn, "team-1", "/repos/holophyte")
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-1", linear_identifier="KO-1",
            title="ticket 1")
        run_id = store.claim(conn, project, ticket, now=1_700_000_000_000)
        conn.execute("DROP TABLE interventions")
        conn.executescript(VERSION_6_INTERVENTIONS_TABLE)
        conn.execute(
            'INSERT INTO interventions (runId, source, "trigger", "action", at)'
            " VALUES (?, 'human', 'manual', 'approve', ?)",
            (run_id, 1_700_000_120_000))
        conn.execute("PRAGMA user_version = 6")
        conn.commit()
        conn.close()
        raw = sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute(
                'INSERT INTO interventions (runId, source, "trigger",'
                ' "action", at) VALUES (?, \'human\', \'manual\','
                ' \'repoint\', 1)', (run_id,))
        raw.close()

        conn = store.open(self.path)
        self.addCleanup(conn.close)

        self.assertGreaterEqual(store.schema.SCHEMA_VERSION, 7)
        self.assertEqual(self.user_version(), store.schema.SCHEMA_VERSION)
        store.record_intervention(conn, run_id, "repoint", "rebuilt")
        self.assertEqual(
            conn.execute('SELECT "action" FROM interventions'
                         " WHERE action != 'migrate' ORDER BY id")
            .fetchall(), [("approve",), ("repoint",)])


class Version9MigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"

    def user_version(self):
        raw = sqlite3.connect(self.path)
        try:
            return raw.execute("PRAGMA user_version").fetchone()[0]
        finally:
            raw.close()

    def test_a_version_9_store_gains_review_round_cap_and_advances_by_one(self):
        """A store stamped 9 has no `runs.reviewRoundCap`; opening it with
        this build adds the column, leaves the run it held with a null
        there, stamps 10, and the cap then lands on a live run (KO-321)."""
        conn = store.open(self.path)
        project = store.tickets.ensure_project(conn, "team-1", "/repos/holophyte")
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-1", linear_identifier="KO-1",
            title="ticket 1")
        run_id = store.claim(conn, project, ticket, now=1_700_000_000_000)
        conn.execute("ALTER TABLE runs DROP COLUMN reviewRoundCap")
        conn.execute("PRAGMA user_version = 9")
        conn.commit()
        conn.close()
        raw = sqlite3.connect(self.path)
        columns = {row[1] for row in raw.execute("PRAGMA table_info(runs)")}
        raw.close()
        self.assertNotIn("reviewRoundCap", columns)
        self.assertEqual(self.user_version(), 9)

        conn = store.open(self.path)
        self.addCleanup(conn.close)

        self.assertGreaterEqual(store.schema.SCHEMA_VERSION, 10)
        self.assertEqual(self.user_version(), store.schema.SCHEMA_VERSION)
        self.assertEqual(
            conn.execute("SELECT id, reviewRoundCap FROM runs").fetchall(),
            [(run_id, None)])
        store.set_review_round_cap(conn, run_id, 4)
        self.assertEqual(
            conn.execute("SELECT reviewRoundCap FROM runs").fetchall(),
            [(4,)])


# `interventions` exactly as schema version 10 shipped it: 'shepherd' in the
# action CHECK, 'reconcile' not yet and no 'linear_completed' trigger.
VERSION_10_INTERVENTIONS_TABLE = VERSION_6_INTERVENTIONS_TABLE.replace(
    "'approve'))", "'approve', 'repoint',\n 'shepherd'))")


class Version10MigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"

    def user_version(self):
        raw = sqlite3.connect(self.path)
        try:
            return raw.execute("PRAGMA user_version").fetchone()[0]
        finally:
            raw.close()

    def test_a_version_10_store_is_rebuilt_to_accept_reconcile(self):
        """A store stamped 10 refuses a 'reconcile' row; opening it with this
        build rebuilds the table in place, keeps the 'shepherd' row it held
        (as 'babysit', the word KO-374 renamed it to), stamps the current
        version, and a reconcile row with the 'linear_completed' trigger
        then lands (KO-329)."""
        conn = store.open(self.path)
        project = store.tickets.ensure_project(conn, "team-1", "/repos/holophyte")
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-1", linear_identifier="KO-1",
            title="ticket 1")
        run_id = store.claim(conn, project, ticket, now=1_700_000_000_000)
        conn.execute("DROP TABLE interventions")
        conn.executescript(VERSION_10_INTERVENTIONS_TABLE)
        conn.execute(
            'INSERT INTO interventions (runId, source, "trigger", "action", at)'
            " VALUES (?, 'human', 'manual', 'shepherd', ?)",
            (run_id, 1_700_000_120_000))
        conn.execute("PRAGMA user_version = 10")
        conn.commit()
        conn.close()
        raw = sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute(
                'INSERT INTO interventions (runId, source, "trigger",'
                ' "action", at) VALUES (?, \'supervisor\', \'manual\','
                ' \'reconcile\', 1)', (run_id,))
        raw.close()

        conn = store.open(self.path)
        self.addCleanup(conn.close)

        self.assertGreaterEqual(store.schema.SCHEMA_VERSION, 11)
        self.assertEqual(self.user_version(), store.schema.SCHEMA_VERSION)
        store.record_intervention(conn, run_id, "reconcile", "Linear completed",
                                  source="supervisor", trigger="linear_completed")
        self.assertEqual(
            conn.execute('SELECT "action", "trigger" FROM interventions'
                         " WHERE action != 'migrate'"
                         " ORDER BY id").fetchall(),
            [("babysit", "manual"), ("reconcile", "linear_completed")])


# `interventions` exactly as schema version 11 shipped it: 'reconcile' and
# the 'linear_completed' trigger in, the daemon's unit actions not yet.
VERSION_11_INTERVENTIONS_TABLE = VERSION_10_INTERVENTIONS_TABLE.replace(
    "'shepherd'))", "'shepherd', 'reconcile'))").replace(
    "'linear_cancelled',", "'linear_cancelled', 'linear_completed',")


class Version11MigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"

    def user_version(self):
        raw = sqlite3.connect(self.path)
        try:
            return raw.execute("PRAGMA user_version").fetchone()[0]
        finally:
            raw.close()

    def test_a_version_11_store_is_rebuilt_to_accept_the_unit_actions(self):
        """Migrate version 11 while preserving its reconcile intervention."""
        conn = store.open(self.path)
        project = store.tickets.ensure_project(conn, "team-1", "/repos/holophyte")
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-1", linear_identifier="KO-1",
            title="ticket 1")
        run_id = store.claim(conn, project, ticket, now=1_700_000_000_000)
        conn.execute("DROP TABLE interventions")
        conn.executescript(VERSION_11_INTERVENTIONS_TABLE)
        conn.execute(
            'INSERT INTO interventions (runId, source, "trigger", "action", at)'
            " VALUES (?, 'supervisor', 'linear_completed', 'reconcile', ?)",
            (run_id, 1_700_000_120_000))
        conn.execute("PRAGMA user_version = 11")
        conn.commit()
        conn.close()
        raw = sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute(
                'INSERT INTO interventions (runId, source, "trigger",'
                ' "action", at) VALUES (?, \'human\', \'manual\','
                ' \'restart_supervisor\', 1)', (run_id,))
        raw.close()

        conn = store.open(self.path)
        self.addCleanup(conn.close)

        self.assertGreaterEqual(store.schema.SCHEMA_VERSION, 12)
        self.assertEqual(self.user_version(), store.schema.SCHEMA_VERSION)
        store.record_intervention(conn, run_id, "restart_supervisor",
                                  "restart asked over HTTP")
        store.record_intervention(conn, run_id, "launch_loop",
                                  "launch asked over HTTP")
        self.assertEqual(
            conn.execute('SELECT "action", "trigger" FROM interventions'
                         " WHERE action != 'migrate'"
                         " ORDER BY id").fetchall(),
            [("reconcile", "linear_completed"), ("restart_supervisor", "manual"),
             ("launch_loop", "manual")])


VERSION_12_INTERVENTIONS_TABLE = VERSION_11_INTERVENTIONS_TABLE.replace(
    "'reconcile'))", "'reconcile', 'restart_supervisor', 'launch_loop'))")


class Version12MigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"

    def user_version(self):
        raw = sqlite3.connect(self.path)
        try:
            return raw.execute("PRAGMA user_version").fetchone()[0]
        finally:
            raw.close()

    def test_a_version_12_store_is_rebuilt_to_accept_config_edit(self):
        """A store stamped 12 refuses a 'config_edit' row; opening it with
        this build rebuilds the table in place, keeps the 'launch_loop' row
        it held, stamps the current version, and the daemon's config edit
        then lands (KO-356)."""
        conn = store.open(self.path)
        project = store.tickets.ensure_project(conn, "team-1", "/repos/holophyte")
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-1", linear_identifier="KO-1",
            title="ticket 1")
        run_id = store.claim(conn, project, ticket, now=1_700_000_000_000)
        conn.execute("DROP TABLE interventions")
        conn.executescript(VERSION_12_INTERVENTIONS_TABLE)
        conn.execute(
            'INSERT INTO interventions (runId, source, "trigger", "action", at)'
            " VALUES (?, 'human', 'manual', 'launch_loop', ?)",
            (run_id, 1_700_000_120_000))
        conn.execute("PRAGMA user_version = 12")
        conn.commit()
        conn.close()
        raw = sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute(
                'INSERT INTO interventions (runId, source, "trigger",'
                ' "action", at) VALUES (?, \'human\', \'manual\','
                ' \'config_edit\', 1)', (run_id,))
        raw.close()

        conn = store.open(self.path)
        self.addCleanup(conn.close)

        self.assertGreaterEqual(store.schema.SCHEMA_VERSION, 13)
        self.assertEqual(self.user_version(), store.schema.SCHEMA_VERSION)
        store.record_intervention(conn, run_id, "config_edit",
                                  "config replaced over HTTP")
        self.assertEqual(
            conn.execute('SELECT "action", "trigger" FROM interventions'
                         " WHERE action != 'migrate'"
                         " ORDER BY id").fetchall(),
            [("launch_loop", "manual"), ("config_edit", "manual")])

# `interventions` exactly as schema version 15 shipped it: 'shepherd' still
# the action a `--babysit` wrote, every daemon action already admitted.
VERSION_15_INTERVENTIONS_TABLE = VERSION_12_INTERVENTIONS_TABLE.replace(
    "'launch_loop'))", "'launch_loop', 'config_edit'))")


class Version15MigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"

    def user_version(self):
        raw = sqlite3.connect(self.path)
        try:
            return raw.execute("PRAGMA user_version").fetchone()[0]
        finally:
            raw.close()

    def test_a_version_15_store_is_rebuilt_with_its_shepherd_rows_renamed(self):
        """A store stamped 15 holds 'shepherd' rows and refuses 'babysit';
        opening it with this build rebuilds the table in place, rewrites
        every 'shepherd' row to 'babysit' with the rest of the row intact,
        stamps the current version, and a babysit row then lands (KO-374)."""
        conn = store.open(self.path)
        project = store.tickets.ensure_project(conn, "team-1", "/repos/holophyte")
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-1", linear_identifier="KO-1",
            title="ticket 1")
        run_id = store.claim(conn, project, ticket, now=1_700_000_000_000)
        conn.execute("DROP TABLE interventions")
        conn.executescript(VERSION_15_INTERVENTIONS_TABLE)
        conn.executemany(
            'INSERT INTO interventions (runId, source, "trigger", "action", at)'
            " VALUES (?, ?, 'manual', ?, ?)",
            [(run_id, "human", "shepherd", 1_700_000_120_000),
             (run_id, "supervisor", "launch_loop", 1_700_000_130_000),
             (run_id, "supervisor", "shepherd", 1_700_000_140_000)])
        conn.execute("PRAGMA user_version = 15")
        conn.commit()
        conn.close()
        raw = sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute(
                'INSERT INTO interventions (runId, source, "trigger",'
                ' "action", at) VALUES (?, \'human\', \'manual\','
                ' \'babysit\', 1)', (run_id,))
        raw.close()

        conn = store.open(self.path)
        self.addCleanup(conn.close)

        self.assertGreaterEqual(store.schema.SCHEMA_VERSION, 16)
        self.assertEqual(self.user_version(), store.schema.SCHEMA_VERSION)
        self.assertEqual(
            conn.execute('SELECT id, source, "action", at FROM interventions'
                         " WHERE action != 'migrate'"
                         " ORDER BY id").fetchall(),
            [(1, "human", "babysit", 1_700_000_120_000),
             (2, "supervisor", "launch_loop", 1_700_000_130_000),
             (3, "supervisor", "babysit", 1_700_000_140_000)])
        store.record_intervention(conn, run_id, "babysit", "look again")
        self.assertEqual(
            conn.execute('SELECT "action" FROM interventions'
                         " WHERE action != 'migrate'"
                         " ORDER BY id DESC LIMIT 1").fetchall(), [("babysit",)])
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                'INSERT INTO interventions (runId, source, "trigger",'
                ' "action", at) VALUES (?, \'human\', \'manual\','
                ' \'shepherd\', 1)', (run_id,))


class OpenRetryTests(unittest.TestCase):
    def test_first_statement_retries_close_connections_then_return_usable_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "store.db")
            connect = sqlite3.connect
            failed = [Mock(), Mock()]
            for conn in failed:
                conn.execute.side_effect = sqlite3.OperationalError("locking protocol")
            with patch("store.schema.sqlite3.connect",
                       side_effect=[*failed, connect(path)]) as opening, \
                    patch("store.schema.time.sleep") as sleep:
                conn = store.open(path)
            self.addCleanup(conn.close)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                             store.schema.SCHEMA_VERSION)
            self.assertEqual(opening.call_count, 3)
            self.assertEqual(sleep.call_args_list, [call(1), call(2)])
            for failed_conn in failed:
                failed_conn.close.assert_called_once()

    def test_attempt_limit_and_nontransient_errors(self):
        for reason, attempts, sleeps in (
                ("locking protocol", 5, [call(1), call(2), call(4), call(8)]),
                ("disk I/O error", 1, [])):
            for at_connect in (False, True):
                with self.subTest(reason=reason, at_connect=at_connect):
                    failed = Mock()
                    failed.execute.side_effect = sqlite3.OperationalError(reason)
                    with patch("store.schema.sqlite3.connect", return_value=failed,
                               side_effect=(sqlite3.OperationalError(reason)
                                            if at_connect else None)) as opening, \
                            patch("store.schema.time.sleep") as sleep:
                        with self.assertRaisesRegex(sqlite3.OperationalError, reason):
                            store.open("unused.db")
                    self.assertEqual(opening.call_count, attempts)
                    self.assertEqual(failed.close.call_count,
                                     0 if at_connect else attempts)
                    self.assertEqual(sleep.call_args_list, sleeps)


class AdmissionMigrationTests(unittest.TestCase):
    def test_version_26_projects_default_to_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            conn = sqlite3.connect(path)
            previous = "\n".join(
                line
                for line in store.schema.SCHEMA.splitlines()
                if not line.strip().startswith(
                    ("admission ", "holdNote ", "CHECK (admission IN"))
            )
            conn.executescript(previous)
            store.ensure_project(conn, "team", "/repo")
            conn.execute("PRAGMA user_version = 26")
            conn.commit()
            conn.close()
            conn = store.open(path)
            try:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone(),
                                 (store.schema.SCHEMA_VERSION,))
                self.assertEqual(
                    conn.execute("SELECT admission, holdNote FROM projects").fetchall(),
                    [("enabled", None)],
                )
                store.hold(conn, 1, "migration supports holds")
            finally:
                conn.close()


class Version26EnumMigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.db"
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        conn.executescript(Path(__file__).with_name("store_v26.sql").read_text())
        conn.executescript("""
            INSERT INTO projects (id, linearTeamId, repoPath, defaultBranch,
                autonomyProfile, activeRunId)
                VALUES (1, 'team', '/repos/project', 'main', 'production', 1);
            INSERT INTO tickets (id, projectId, linearIssueId, linearIdentifier,
                title, status, affinity, mirroredAt, activeRunId, lastRunId)
                VALUES (1, 1, 'issue', 'KO-1', 'title', 'in_flight', 'gui', 1, 1, 1);
            INSERT INTO runs (id, ticketId, projectId, attempt, phase, startedAt,
                lastHeartbeat, outcome, outcomeClass, resumePhase)
                VALUES (1, 1, 1, 1, 'failed', 1, 2, 'failed', 'infra', 'reviewing');
            INSERT INTO reviewRounds (id, runId, round, verdict,
                findingsFingerprint, reviewerModel, startedAt)
                VALUES (1, 1, 1, 'changes_requested', 'fingerprint', 'reviewer', 1);
            INSERT INTO runEvents (id, runId, seq, level, kind, summary, at)
                VALUES (1, 1, 1, 'detail', 'test', 'event', 1);
            INSERT INTO ledger (id, runId, ticketId, at, kind, text, source)
                VALUES (1, 1, 1, 1, 'adjudication', 'ADDRESS', 'operator');
            INSERT INTO interventions (id, runId, source, "trigger", action, at)
                VALUES (1, 1, 'human', 'review_stuck', 'redirect', 1);
            CREATE INDEX enum_migration_index ON tickets(title);
        """)
        self.before = self.rows(conn)
        self.old_schema = conn.execute(
            "SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
        conn.close()

    @staticmethod
    def rows(conn):
        return {table: conn.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
                for table in dict.fromkeys(
                    t for t, _ in store.enums.CONSTRAINED_COLUMNS)}

    def test_rows_survive_and_each_enum_still_rejects_invalid_inserts(self):
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        after = self.rows(conn)
        # Opening adds one truthful migration-evidence row, as every version does.
        after['interventions'] = after['interventions'][:1]
        # Admission columns are new; all pre-existing project values survive.
        after['projects'] = [row[:7] + row[9:] for row in after['projects']]
        columns = [r[1] for r in conn.execute('PRAGMA table_info(runs)')]
        after['runs'] = [tuple(value for column, value in zip(columns, row)
                               if column not in {'parkKind', 'failureKind',
                                                 'stopRequested', 'workerPid',
                                                 'prSeenTitle', 'verifyMs',
                                                 'verifyStartedAt'})
                         for row in after['runs']]
        self.assertEqual(after, self.before)
        self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],
                         store.schema.SCHEMA_VERSION)
        self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertIsNotNone(conn.execute(
            "SELECT sql FROM sqlite_master"
            " WHERE name = 'enum_migration_index'").fetchone())
        from tests.test_store_enums import enum_checks
        self.assertEqual(enum_checks(conn), {
            key: store.enums.check_clause(key[1], enum)
            for key, enum in store.enums.CONSTRAINED_COLUMNS.items()})
        # Clone a populated row while avoiding unrelated UNIQUE constraints.
        replacements = {'id': 'NULL', 'linearTeamId': "'new-team'",
                        'linearIssueId': "'new-issue'", 'attempt': '999',
                        'round': '999', 'seq': '999'}
        for table, column in store.enums.CONSTRAINED_COLUMNS:
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            expressions = ["?" if c == column else replacements.get(c, f'"{c}"')
                           for c in columns]
            sql = (f'INSERT INTO "{table}" SELECT ' + ', '.join(expressions)
                   + f' FROM "{table}" WHERE id = 1')
            with self.subTest(table=table, column=column):
                conn.execute('SAVEPOINT enum_insert')
                for member in store.enums.CONSTRAINED_COLUMNS[table, column]:
                    conn.execute(sql, (member.value,))
                    conn.execute('ROLLBACK TO enum_insert')
                with self.assertRaisesRegex(sqlite3.IntegrityError,
                                            'CHECK constraint failed'):
                    conn.execute(sql, ('outside-enum',))
                conn.execute('ROLLBACK TO enum_insert')
                conn.execute('RELEASE enum_insert')

    def test_failed_rebuild_rolls_back_rows_schema_and_version(self):
        with patch('store.schema._record_migration', side_effect=RuntimeError('stop')):
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                store.open(self.path)
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(self.rows(conn), self.before)
        self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 26)
        self.assertEqual(conn.execute(
            'SELECT name, sql FROM sqlite_master ORDER BY name').fetchall(),
            self.old_schema)


class ParkKindMigrationTests(unittest.TestCase):
    def test_previous_store_backfills_only_current_parked_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            conn = sqlite3.connect(path)
            conn.executescript("\n".join(
                line for line in store.schema.SCHEMA.splitlines()
                if not line.strip().startswith("parkKind ")))
            project = store.tickets.ensure_project(conn, "team", "/repo")
            for n, question in enumerate(("PR open: URL\nready", "rejected: URL",
                                          "Please help", "PR open: stale"), 1):
                conn.execute("INSERT INTO tickets (id, projectId, linearIssueId,"
                             " linearIdentifier, title, status, mirroredAt, affinity,"
                             " blockedQuestion) VALUES (?, ?, ?, ?,"
                             " 'old', ?, 1, 'any', ?)",
                             (n, project, str(n), f"KO-{n}",
                              "blocked_on_operator" if n < 4 else "ready", question))
                conn.execute("INSERT INTO runs (id, ticketId, projectId,"
                             " attempt, phase,"
                             " startedAt, lastHeartbeat) VALUES (?, ?, ?, 1, ?, 1, 1)",
                             (n, n, project, "rejected" if n == 2
                              else "awaiting_merge_approval"))
                conn.execute("UPDATE tickets SET lastRunId = ? WHERE id = ?", (n, n))
            conn.execute("PRAGMA user_version = 29")
            conn.commit()
            conn.close()
            conn = store.open(path)
            self.addCleanup(conn.close)
            self.assertEqual(conn.execute(
                "SELECT parkKind FROM runs ORDER BY id").fetchall(),
                             [("pull_request",), ("pull_request_closed",),
                              ("question",), (None,)])
            conn.execute("UPDATE tickets SET blockedQuestion = 'new wording'")
            conn.commit()
            store.init(conn)
            self.assertEqual(conn.execute(
                "SELECT parkKind FROM runs WHERE id = 1").fetchone(),
                             ("pull_request",))


if __name__ == "__main__":
    unittest.main()
