"""Schema bootstrap contract for the v2 store.

The expected table and column names below are transcribed from
docs/v2/state-model.md §1-§2 (plus the §7 lease column) by hand, on purpose:
reading them back out of store.SCHEMA would only prove the module agrees with
itself. This transcription is the independent oracle.

Run: python3 -m unittest discover -s tests -p 'test_store*' -v
"""
from __future__ import annotations

import socket
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
        # Store-owned: the merge commit a merged run landed on main as, so
        # the ticket-to-commit link is a column and not a grep of git log.
        "mergeSha",
        # Store-owned, not a documented field: §5 requires a resume to
        # "re-enter the phase it left" and leaves the mechanism to us, so
        # `resume()` reads the parked phase from this column.
        "resumePhase",
        # Store-owned too: the ticket's estimate as it stood at the claim, so
        # a finished run's estimate-vs-actual does not move when the ticket's
        # own `timeBoxMs` is later re-mirrored.
        "timeBoxMs",
        # Store-owned as well: the ticket's contract frozen at the claim, so
        # the merge gate can tell a body edited mid-run from the one the run
        # was worked to.
        "ticketSnapshot",
        # Store-owned: whether a failure is evidence about the ticket
        # (`work`) or about the factory's own plumbing (`infra`), so the
        # escalation count can leave the second kind out.
        "outcomeClass",
        # Store-owned: the hostname that claimed the run, so a store read on
        # another machine can say where each live run is executing.
        "host",
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
    # Store-owned, not a documented table: the supervisor sweep's per-run
    # strike tally, which exists because "silent on two consecutive sweeps"
    # has to survive between two sweep invocations.
    "sweepStrikes": {"runId", "strikes", "lastSeen"},
    # Store-owned as well: one row per `--supervise` process, bumped on every
    # pass, so a reader can tell a live watcher from a dead one.
    "supervisorHeartbeats": {"pid", "startedAt", "lastBeat", "passes", "host"},
    # And one row per self-merge re-exec of the loop, so the sweep can tell a
    # restart that came back from one that died in the exec.
    "loopRestarts": {"id", "projectId", "sha", "at", "returnedAt",
                     "reportedAt"},
}

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

    def raw(self):
        """A plain sqlite3 connection to the file, bypassing store.open()."""
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        return conn

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
        project = store.ensure_project(conn, "team-h", "/repos/x")
        ticket = store.mirror_ticket(
            conn, project, linear_issue_id="iss-h", linear_identifier="KO-9",
            title="a ticket", acceptance_criteria=["Given, then"],
            verification_commands=["echo ok"], time_box_ms=None)
        store.transition(conn, ticket, "in_flight")

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

    def review_round_count(self, conn, run_id):
        (count,) = conn.execute(
            "SELECT reviewRoundCount FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return count

    def a_review_round(self, conn, run_id, round):  # noqa: A002 - the column's name
        """One `reviewRounds` row on `run_id`, for the backfill tests."""
        conn.execute(
            "INSERT INTO reviewRounds"
            " (runId, round, verdict, findingsFingerprint, reviewerModel,"
            "  startedAt)"
            " VALUES (?, ?, 'pass', 'fp', 'a-model', 0)",
            (run_id, round),
        )
        conn.commit()

    def a_run(self, conn, phase="working", ended_at=None):
        """One project, ticket and run in `phase`, for the migration tests.

        Each call makes its own project and ticket, numbered so that two
        runs in one test do not collide on `projects.linearTeamId` or
        `tickets.linearIssueId`, both of which are UNIQUE.
        """
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


if __name__ == "__main__":
    unittest.main()


# The three indexes the ticket names, by the column each one covers. Named
# here by hand rather than read from store.INDEXES: the point is that these
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

    def raw(self):
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        return conn

    def current_unstamped_store(self):
        """A store with every current column and `user_version` still 0.

        Exactly what any file made before the stamp existed looks like:
        store.SCHEMA ran, ADDED_COLUMNS ran, nothing recorded which.
        """
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

        self.assertEqual(self.user_version(), store.SCHEMA_VERSION)
        self.assertEqual(conn.execute("SELECT * FROM projects").fetchall(),
                         before)

    def test_a_version_3_store_gains_merge_sha_and_keeps_its_rows(self):
        """A store stamped 3 has no `runs.mergeSha`; opening it with this
        build adds the column, leaves the run it held with a null there,
        and stamps version 4."""
        conn = store.open(self.path)
        store.init(conn)
        project = store.ensure_project(conn, "team-1", "/repos/holophyte")
        ticket = store.mirror_ticket(
            conn, project, linear_issue_id="issue-1", linear_identifier="KO-1",
            title="ticket 1")
        run_id = store.claim(conn, project, ticket, now=1_700_000_000_000)
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

        self.assertEqual(store.SCHEMA_VERSION, 4)
        self.assertEqual(self.user_version(), 4)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        self.assertIn("mergeSha", columns)
        self.assertEqual(
            conn.execute("SELECT id, outcome, mergeSha FROM runs").fetchall(),
            [(run_id, "merged", None)])

    def test_a_store_stamped_newer_is_refused_and_untouched(self):
        self.current_unstamped_store()
        newer = store.SCHEMA_VERSION + 1
        raw = self.raw()
        raw.execute(f"PRAGMA user_version = {newer}")
        raw.commit()
        raw.close()
        before = self.path.read_bytes()

        with self.assertRaises(SystemExit) as caught:
            store.open(self.path)

        message = str(caught.exception)
        self.assertIn(str(newer), message)
        self.assertIn(str(store.SCHEMA_VERSION), message)
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
