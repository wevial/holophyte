"""Each schema migration carries an older store forward with its rows."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import store
import store.schema
import store.tickets
from tests.schema_fixture import VERSION_4_INTERVENTIONS_TABLE
from tests.ticket_url_fixture import assert_schema_url


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


STORY_TABLES = ('stories', 'storyChildren', 'witnessResults', 'storyDecisions')


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
                for table in dict.fromkeys(t for t, _ in store.enums.CONSTRAINED_COLUMNS
                                           if t not in ('ticketRevisions',
                                                        'gapLayers',
                                                        *STORY_TABLES))}

    def test_rows_survive_and_each_enum_still_rejects_invalid_inserts(self):
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        after = self.rows(conn)
        # Opening adds one truthful migration-evidence row, as every version does.
        after['interventions'] = after['interventions'][:1]
        # Admission and board columns are new; pre-existing values survive.
        after['projects'] = [row[:7] + row[9:-1] for row in after['projects']]
        after['tickets'] = [row[:-11] for row in after['tickets']]
        columns = [r[1] for r in conn.execute('PRAGMA table_info(runs)')]
        added = {'parkKind', 'failureKind', 'stopRequested', 'workerPid', 'revision',
                 'prSeenTitle', 'verifyMs', 'verifyStartedAt', 'storyGeneration'}
        after['runs'] = [tuple(value for column, value in zip(columns, row)
                               if column not in added) for row in after['runs']]
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
        # The migration leaves gapLayers empty; give it a row to clone.
        conn.execute("INSERT INTO gapLayers (ticketId, layer, note, author, at)"
                     " VALUES (1, 'none', 'lesson', 'test', 1)")
        # Clone a populated row while avoiding unrelated UNIQUE constraints.
        replacements = {'id': 'NULL', 'linearTeamId': "'new-team'",
                        'linearIssueId': "'new-issue'", 'attempt': '999',
                        'round': '999', 'seq': '999', 'revision': '999'}
        for table, column in store.enums.CONSTRAINED_COLUMNS:
            if table in STORY_TABLES:
                continue  # empty; test_store_stories_schema inserts into them
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            expressions = ["?" if c == column else replacements.get(c, f'"{c}"')
                           for c in columns]
            sql = (f'INSERT INTO "{table}" SELECT ' + ', '.join(expressions)
                   + f' FROM "{table}" WHERE rowid = 1')
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


class RebuildKeepsForeignKeysTests(unittest.TestCase):
    """A rebuild that renamed the live table away made SQLite rewrite every
    key pointing at it to the `_old` name, then dropped that table: on
    2026-09-22 `runs.stopRequested` was left referencing
    `interventions_old` and no claim could create a run (KO-664)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"
        conn = store.open(self.path)
        self.project_id = store.tickets.ensure_project(conn, "team-1", "/repos/h")
        conn.commit()
        conn.close()

    def rebuild_on_open(self, *statements):
        """Apply `statements` to the store as an older build left it, stamp
        it one version back and reopen it with this build."""
        raw = sqlite3.connect(self.path)
        raw.executescript("".join(f"{sql};\n" for sql in statements))
        raw.execute(f"PRAGMA user_version = {store.schema.SCHEMA_VERSION - 1:d}")
        raw.commit()
        raw.close()
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        return conn

    @staticmethod
    def parent_of(conn, table, column):
        return [row[2] for row in conn.execute(
            f"PRAGMA foreign_key_list({table})") if row[3] == column]

    def test_widening_interventions_keeps_runs_referencing_it(self):
        raw = sqlite3.connect(self.path)
        (ddl,) = raw.execute("SELECT sql FROM sqlite_master"
                             " WHERE name = 'interventions'").fetchone()
        raw.close()
        self.assertIn("'abort_close'", ddl)
        narrowed = ddl.replace(", 'abort_close'", "")
        self.assertNotIn("'abort_close'", narrowed)

        conn = self.rebuild_on_open("DROP TABLE interventions", narrowed)

        self.assertIn("'abort_close'", conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'interventions'"
        ).fetchone()[0])
        self.assertEqual(self.parent_of(conn, "runs", "stopRequested"),
                         ["interventions"])
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone(), (1,))
        ticket = store.tickets.mirror_ticket(
            conn, self.project_id, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="ticket 1")
        run_id = store.claim(conn, self.project_id, ticket, now=1_700_000_000_000)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs WHERE id = ?",
                                      (run_id,)).fetchone(), (1,))

    def test_rebuilding_run_events_keeps_references_to_it(self):
        raw = sqlite3.connect(self.path)
        (ddl,) = raw.execute("SELECT sql FROM sqlite_master"
                             " WHERE name = 'runEvents'").fetchone()
        raw.close()
        required = ddl.replace("runId   INTEGER REFERENCES runs (id)",
                               "runId   INTEGER NOT NULL REFERENCES runs (id)")
        self.assertNotEqual(required, ddl)

        conn = self.rebuild_on_open(
            "DROP TABLE runEvents", required,
            "CREATE TABLE eventNotes (id INTEGER PRIMARY KEY,"
            " eventId INTEGER REFERENCES runEvents (id))")

        self.assertFalse(any(row[1] == "runId" and row[3] for row in
                             conn.execute("PRAGMA table_info(runEvents)")))
        self.assertEqual(self.parent_of(conn, "eventNotes", "eventId"),
                         ["runEvents"])
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    @staticmethod
    def run_row(conn, run_id):
        cursor = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        names = [column[0] for column in cursor.description]
        return dict(zip(names, cursor.fetchone()))

    def test_an_older_store_admits_the_not_reproduced_park(self):
        """KO-657: the enum rebuild widens `runs.parkKind`, keeping the row.

        A store stamped 35 -- the version just below 36, the last whose
        CHECK lacked the value -- and an older one stamped 33 both open at
        this build's version."""
        for version in (33, 35):
            with self.subTest(version=version):
                path = self.path.with_name(f"v{version}.sqlite3")
                conn = store.open(path)
                project = store.tickets.ensure_project(conn, "team-1", "/r")
                ticket = store.tickets.mirror_ticket(
                    conn, project, linear_issue_id="issue-1",
                    linear_identifier="KO-1", title="ticket 1")
                run_id = store.claim(conn, project, ticket, now=1_700_000_000_000)
                for phase in ("working", "verifying", "reviewing"):
                    store.set_phase(conn, run_id, phase)
                store.park(conn, run_id, "awaiting_merge_approval", "merge?",
                           candidate_sha="a" * 40)
                before = self.run_row(conn, run_id)
                (ddl,) = conn.execute("SELECT sql FROM sqlite_master"
                                      " WHERE name = 'runs'").fetchone()
                conn.close()
                narrowed = ddl.replace(", 'not_reproduced'", "")
                self.assertNotIn("'not_reproduced'", narrowed)
                old = narrowed.replace('CREATE TABLE "runs" (',
                                       "CREATE TABLE runs_old (", 1)
                self.assertNotEqual(old, narrowed)
                raw = sqlite3.connect(path)
                raw.executescript(f"{old};\nINSERT INTO runs_old SELECT * FROM runs;\n"
                                  "DROP TABLE runs;\n"
                                  "ALTER TABLE runs_old RENAME TO runs;\n"
                                  f"PRAGMA user_version = {version:d};\n")
                raw.close()

                conn = store.open(path)
                self.addCleanup(conn.close)

                self.assertEqual(conn.execute("PRAGMA user_version").fetchone(),
                                 (store.schema.SCHEMA_VERSION,))
                self.assertEqual(self.run_row(conn, run_id), before)
                conn.execute("UPDATE runs SET parkKind = 'not_reproduced'"
                             " WHERE id = ?", (run_id,))
                self.assertEqual(self.run_row(conn, run_id)["parkKind"],
                                 "not_reproduced")

    def test_open_refuses_a_schema_referencing_a_missing_table(self):
        raw = sqlite3.connect(self.path)
        # Not WAL, so the switch open() would make is a header write too.
        raw.execute("PRAGMA journal_mode = DELETE")
        raw.execute("CREATE TABLE strays (id INTEGER PRIMARY KEY,"
                    " ghostId INTEGER REFERENCES ghosts (id))")
        # A table the migration itself would create: refusing must not
        # leave it behind either.
        raw.execute("DROP TABLE loopRestarts")
        raw.execute(f"PRAGMA user_version = {store.schema.SCHEMA_VERSION - 1:d}")
        raw.commit()
        raw.close()
        before = self.path.read_bytes()

        with self.assertRaisesRegex(store.schema.SchemaError,
                                    r"strays\.ghostId.*\bghosts\b"):
            store.open(self.path)

        self.assertEqual(self.path.read_bytes(), before)
        raw = sqlite3.connect(self.path)
        self.addCleanup(raw.close)
        self.assertEqual(raw.execute("PRAGMA journal_mode").fetchone(),
                         ("delete",))
