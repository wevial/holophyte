from __future__ import annotations

import contextlib
import getpass
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import enums as _enums
from .ddl import _INTERVENTIONS_DDL, INDEXES, SCHEMA

# Both literals, never expressions: `fetched_schema()` in
# holophyte/loop/pool_handoff.py reads them with `ast.literal_eval`.
SCHEMA_VERSION = 40

# The oldest version whose builds can still read and write a store at
# SCHEMA_VERSION. On each bump keep it for an additive change, else raise it
# to the new version. Additive: a new table or index, a nullable column or
# one NOT NULL with a DEFAULT, a backfill of only the columns the bump adds,
# an enum value on a column an older build never branches on. Not additive:
# a dropped or renamed column or table, a new or tightened constraint on an
# existing column, an enum value removed or renamed, or one added to
# `runs.phase`, `projects.admission`, `tickets.status` or `runs.parkKind`.
READABLE_FROM = 40

BUSY_TIMEOUT_S = 30


class SchemaNewer(SystemExit):
    def __init__(self, path, version, floor=None):
        self.version = version
        self.floor = floor
        found = ("it records no readable-from floor" if floor is None
                 else f"it is readable from version {floor} on")
        super().__init__(
            f"{path}: store schema version {version} is newer than the"
            f" version {SCHEMA_VERSION} this build understands; refusing"
            f" to open it with an older factory ({found})")


class SchemaOlder(SystemExit):
    def __init__(self, path, version, expected):
        self.version = version
        super().__init__(
            f"store at {path} is schema {version}; this build expects {expected};"
            " start the loop or the serve daemon to migrate it, or run the"
            " command from the build that wrote it")


class SchemaError(sqlite3.DatabaseError):
    def __init__(self, dangling):
        self.dangling = dangling
        super().__init__("store schema references missing tables: " + "; ".join(
            f"{table}.{column} references {parent}, which does not exist"
            for table, column, parent in dangling))


def _refuse_dangling_references(conn):
    tables = [name for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")]
    present = {name.lower() for name in tables}
    dangling = [
        (table, row[3], row[2])
        for table in tables
        for row in conn.execute(f'PRAGMA foreign_key_list("{table}")')
        if row[2].lower() not in present]
    if dangling:
        raise SchemaError(dangling)


class _Connection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()


def _connect_with_version(path):
    for attempt in range(5):
        conn = None
        try:
            conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S,
                                   check_same_thread=False, factory=_Connection)
            (version,) = conn.execute("PRAGMA user_version").fetchone()
            return conn, version
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
            if str(exc) != "locking protocol" or attempt == 4:
                raise
            time.sleep(2 ** attempt)


def latest_migration_note(conn):
    if "note" not in {r[1] for r in conn.execute("PRAGMA table_info(interventions)")}:
        return None
    row = conn.execute(
        "SELECT note FROM interventions WHERE action = 'migrate'"
        " AND note IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    return None if row is None else row[0]


def _readable_from(conn, version):
    note = latest_migration_note(conn)
    try:
        detail = json.loads(note) if note is not None else {}
    except ValueError:
        return None
    if not isinstance(detail, dict) or detail.get("to") != version:
        return None
    floor = detail.get("readableFrom")
    return floor if isinstance(floor, int) else None


def open(path, *, migrate=True):
    # Refused before anything writes, so a newer build's store stays as it was.
    conn, version = _connect_with_version(path)
    newer = version > SCHEMA_VERSION
    if newer:
        floor = _readable_from(conn, version)
        if floor is None or floor > SCHEMA_VERSION:
            conn.close()
            raise SchemaNewer(path, version, floor)
    # Per connection in SQLite, so asserted on every open.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_S * 1000}")
    try:
        if not migrate:
            if version < SCHEMA_VERSION:
                raise SchemaOlder(path, version, SCHEMA_VERSION)
        elif version < SCHEMA_VERSION:
            init(conn)
        # After migrating: an older store may name a table only the ladder creates.
        _refuse_dangling_references(conn)
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode.lower() != "wal":
            raise sqlite3.DatabaseError(
                f"{path}: could not enable WAL mode (journal_mode is {mode!r})")
        if migrate and not newer:
            conn.executescript(INDEXES)
    except BaseException:
        conn.close()
        raise
    return conn


# CREATE TABLE IF NOT EXISTS never extends an existing table, so every column
# added to SCHEMA needs its clause here too, CHECK included.
ADDED_COLUMNS = (
    ("runs", "stopRequested", "stopRequested INTEGER REFERENCES interventions(id)"),
    ("runs", "workerPid", "workerPid INTEGER"),
    ("runs", "parkKind", "parkKind TEXT "
     + _enums.check_clause("parkKind", _enums.ParkKind)),
    ('runs', 'failureKind', 'failureKind TEXT '
     + _enums.check_clause('failureKind', _enums.FailureKind)),
    ("projects", "admission", "admission TEXT NOT NULL DEFAULT 'enabled' "
     + _enums.check_clause("admission", _enums.ProjectAdmission)),
    ("projects", "holdNote", "holdNote TEXT"),
    ("runs", "approvedAt", "approvedAt INTEGER"),
    ("runs", "approvedBy", "approvedBy TEXT"),
    ("tickets", "boardState", "boardState TEXT"),
    ("tickets", "url", "url TEXT"),
    ("projects", "launchBackoffUntil", "launchBackoffUntil INTEGER"),
    ("projects", "launchBackoffReason", "launchBackoffReason TEXT"),
    ("runEvents", "projectId", "projectId INTEGER REFERENCES projects (id)"),
    ("interventions", "note", "note TEXT"),
    ("interventions", "projectId", "projectId INTEGER REFERENCES projects (id)"),
    ("runs", "workingMs", "workingMs INTEGER"),
    ("runs", "workStartedAt", "workStartedAt INTEGER"),
    ("runs", "verifyMs", "verifyMs INTEGER"),
    ("runs", "verifyStartedAt", "verifyStartedAt INTEGER"),
    (
        "runs",
        "timeBoxMs",
        "timeBoxMs INTEGER",
    ),
    (
        "runs",
        "resumePhase",
        "resumePhase TEXT " + _enums.check_clause("resumePhase", _enums.ResumePhase),
    ),
    (
        "runs",
        "ticketSnapshot",
        "ticketSnapshot TEXT",
    ),
    (
        "runs",
        "outcomeClass",
        "outcomeClass TEXT NOT NULL DEFAULT 'work'"
        " " + _enums.check_clause("outcomeClass", _enums.OutcomeClass),
    ),
    (
        "runs",
        "host",
        "host TEXT",
    ),
    (
        "supervisorHeartbeats",
        "host",
        "host TEXT",
    ),
    (
        "runs",
        "mergeSha",
        "mergeSha TEXT",
    ),
    (
        "runs",
        "candidateSha",
        "candidateSha TEXT",
    ),
    (
        "runs",
        "approvedSha",
        "approvedSha TEXT",
    ),
    (
        "runs",
        "reviewRoundCap",
        "reviewRoundCap INTEGER",
    ),
    (
        "tickets",
        "body",
        "body TEXT NOT NULL DEFAULT ''",
    ),
    (
        "runs",
        "prSeenAt",
        "prSeenAt TEXT",
    ),
    (
        "runs",
        "prSeenThreads",
        "prSeenThreads INTEGER",
    ),
    (
        "runs",
        "prSeenChecks",
        "prSeenChecks TEXT",
    ),
    (
        "runs",
        "prSeenReview",
        "prSeenReview TEXT",
    ),
    (
        "runs",
        "prSeenTitle",
        "prSeenTitle TEXT",
    ),
    (
        "projects",
        "boardAskedAt",
        "boardAskedAt INTEGER",
    ),
    ("projects", "ticketSeq", "ticketSeq INTEGER NOT NULL DEFAULT 0"),
    ("tickets", "boardColumn", "boardColumn TEXT "
     + _enums.check_clause("boardColumn", _enums.BoardColumn)),
    ("tickets", "priority", "priority INTEGER"),
    ("tickets", "labels", "labels TEXT NOT NULL DEFAULT '[]'"),
    ("tickets", "filedAt", "filedAt INTEGER"),
    ("tickets", "boardUpdatedAt", "boardUpdatedAt INTEGER"),
    ("tickets", "revision", "revision INTEGER NOT NULL DEFAULT 0"),
    ("tickets", "pushState", "pushState TEXT"),
    ("tickets", "pushFrom", "pushFrom TEXT"),
    ("tickets", "pushAt", "pushAt INTEGER"),
    ("tickets", "goneSince", "goneSince INTEGER"),
    ("runs", "revision", "revision INTEGER"),
    ("tickets", "parentTicketId",
     "parentTicketId INTEGER REFERENCES tickets (id)"),
    ("runs", "storyGeneration", "storyGeneration INTEGER"),
    ("gapLayers", "foundBy", "foundBy TEXT NOT NULL DEFAULT 'operator' "
     + _enums.check_clause("foundBy", _enums.GapFinder)),
)


# Each statement matches only rows it would change, so `init()` stays idempotent.
BACKFILLS = (
    (
        "runs.reviewRoundCount on runs that ended before close-out stamped it",
        "UPDATE runs SET reviewRoundCount ="
        "     (SELECT COUNT(*) FROM reviewRounds"
        "      WHERE runId = runs.id AND verdict != 'error')"
        " WHERE endedAt IS NOT NULL"
        "   AND reviewRoundCount <>"
        "     (SELECT COUNT(*) FROM reviewRounds"
        "      WHERE runId = runs.id AND verdict != 'error')",
    ),
)


def init(conn):
    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    # `executescript` commits whatever is pending first, so the BEGIN opens
    # the script and the whole ladder rolls back together.
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute(_INTERVENTIONS_DDL)
        for table, column, ddl in ADDED_COLUMNS:
            columns = {row[1]
                       for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        for _repairs, sql in BACKFILLS:
            conn.execute(sql)
        _widen_runs_outcomes(conn)
        _widen_interventions_action(conn)
        _project_startup_events(conn)
        if version < 29:
            from .failure_kinds import backfill
            backfill(conn)
        if version < 31:
            conn.execute("UPDATE runs SET parkKind = (SELECT CASE"
                         " WHEN blockedQuestion GLOB 'PR open:*' THEN 'pull_request'"
                         " WHEN blockedQuestion GLOB 'rejected:*'"
                         " THEN 'pull_request_closed'"
                         " ELSE 'question' END FROM tickets t"
                         " WHERE t.lastRunId = runs.id"
                         " AND t.status = 'blocked_on_operator')"
                         " WHERE parkKind IS NULL")
        if version < 40:
            _rebuild_enum_tables(conn)
        if version < 37:
            conn.execute(
                "INSERT INTO ticketRevisions (ticketId, revision, at, author,"
                " title, body, priority, labels, boardColumn)"
                " SELECT id, 1, ?, 'backfill', title, body, priority, labels,"
                " boardColumn FROM tickets WHERE revision = 0",
                (int(time.time() * 1000),))
            conn.execute("UPDATE tickets SET revision = 1 WHERE revision = 0")
        if version < SCHEMA_VERSION:
            _record_migration(conn, version)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
        _refuse_dangling_references(conn)
        if conn.execute("PRAGMA foreign_key_check").fetchall() != foreign_key_errors:
            raise sqlite3.IntegrityError("foreign key violation during migration")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute(f"PRAGMA foreign_keys = {foreign_keys}")


def _rebuild_enum_tables(conn):
    for table in dict.fromkeys(table for table, _ in _enums.CONSTRAINED_COLUMNS):
        # `_widen_interventions_action()` rebuilds it and translates its history.
        if table == "interventions":
            continue
        indexes = conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name = ?"
            " AND type IN ('index', 'trigger') AND sql IS NOT NULL", (table,)
        ).fetchall()
        ddl = SCHEMA.split(f"CREATE TABLE IF NOT EXISTS {table} (", 1)[1]
        ddl = ddl.split(");", 1)[0]
        conn.execute(f"CREATE TABLE {table}_enum_new (" + ddl + ")")
        columns = ", ".join(f'"{row[1]}"' for row in conn.execute(
            f"PRAGMA table_info({table})"))
        conn.execute(f"INSERT INTO {table}_enum_new ({columns})"
                     f" SELECT {columns} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_enum_new RENAME TO {table}")
        for (sql,) in indexes:
            conn.execute(sql)


def _widen_runs_outcomes(conn):
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'runs'")\
        .fetchone()[0]
    if "'rejected'" in ddl.partition("CHECK (phase IN (")[2].partition(")")[0]:
        return
    indexes = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'index'"
                           " AND tbl_name = 'runs' AND sql IS NOT NULL").fetchall()
    with _transaction(conn):
        widened = ddl.replace('CREATE TABLE "runs"', "CREATE TABLE runs_new", 1)
        widened = widened.replace("CREATE TABLE runs (", "CREATE TABLE runs_new (", 1)
        widened = widened.replace("'failed'", "'failed', 'rejected'")
        conn.execute(widened)
        conn.execute("INSERT INTO runs_new SELECT * FROM runs")
        conn.execute("DROP TABLE runs")
        conn.execute("ALTER TABLE runs_new RENAME TO runs")
        for (sql,) in indexes:
            conn.execute(sql)


def _record_migration(conn, version):
    try:
        build = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        build = "unknown"
    at = int(time.time() * 1000)
    note = json.dumps({"from": version, "to": SCHEMA_VERSION,
                       "readableFrom": READABLE_FROM, "build": build,
                       "pid": os.getpid(), "ppid": os.getppid(),
                       "argv": sys.argv, "user": getpass.getuser(), "at": at})
    conn.execute(
        'INSERT INTO interventions (source, "trigger", action, note, at)'
        " VALUES ('factory', 'manual', 'migrate', ?, ?)", (note, at))


def _widen_interventions_action(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table'"
        " AND name = 'interventions'").fetchone()
    if row is None:
        return
    # Only the action CHECK's own list: the literal elsewhere must not skip a rebuild.
    (ddl,) = row
    admitted = ddl.partition('"action" IN (')[2].partition(")")[0]
    if all(value in admitted
           for value in ("'repoint'", "'babysit'", "'reconcile'", "'operator_note'",
                         "'restart_supervisor'", "'launch_loop'",
                         "'config_edit'", "'launch_backoff'", "'route_fallback'",
                         "'migrate'", "'hold'", "'release_hold'",
                         "'register_project'", "'disable'", "'pause'",
                         "'abort'", "'abort_close'", "'approve_story'",
                         "'decide'")):
        return
    (orphans,) = conn.execute(
        "SELECT COUNT(*) FROM interventions i LEFT JOIN runs r"
        " ON r.id = i.runId WHERE r.id IS NULL AND i.runId IS NOT NULL").fetchone()
    if orphans:
        raise sqlite3.IntegrityError(
            f"{orphans} interventions row(s) reference runs that do not"
            " exist; repair them before this store can migrate")
    # Built beside and renamed in: SQLite rewrites keys that name a renamed
    # table, so `runs.stopRequested` would follow it to the dropped name.
    with _transaction(conn):
        conn.execute(_INTERVENTIONS_DDL.replace(
            "CREATE TABLE IF NOT EXISTS interventions (",
            "CREATE TABLE interventions_new (", 1))
        conn.execute(
            "INSERT INTO interventions_new"
            ' (id, runId, source, "trigger", "action", question, guidance, at,'
            ' projectId, note)'
            ' SELECT id, runId, source, "trigger",'
            "   CASE \"action\" WHEN 'shepherd' THEN 'babysit'"
            '   ELSE "action" END, question, guidance, at, projectId, note'
            " FROM interventions")
        conn.execute("DROP TABLE interventions")
        conn.execute("ALTER TABLE interventions_new RENAME TO interventions")


@contextlib.contextmanager
def _transaction(conn):
    with getattr(conn, "_lock", contextlib.nullcontext()):
        if conn.in_transaction:
            yield
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            # Inside the guard: a failed deferred-constraint COMMIT leaves the
            # transaction open, and the next caller would join it.
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


@contextlib.contextmanager
def transaction(conn):
    with _transaction(conn):
        yield


def _project_startup_events(conn):
    columns = conn.execute("PRAGMA table_info(runEvents)").fetchall()
    if not any(row[1] == "runId" and row[3] for row in columns):
        return
    ddl = SCHEMA.split("CREATE TABLE IF NOT EXISTS runEvents (", 1)[1].split(
        ");", 1)[0]
    conn.execute("CREATE TABLE runEvents_new (" + ddl + ")")
    conn.execute(
        "INSERT INTO runEvents_new (id, runId, seq, level, kind, summary,"
        " payload, at, projectId) SELECT id, runId, seq, level, kind, summary,"
        " payload, at, projectId FROM runEvents")
    conn.execute("DROP TABLE runEvents")
    conn.execute("ALTER TABLE runEvents_new RENAME TO runEvents")
