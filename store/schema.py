"""store.schema: the store's schema, its migration ladder and the connection."""
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

# One statement per table, in dependency order where it matters. Every
# statement is IF NOT EXISTS, which is the whole of init()'s idempotency:
# re-running it on a populated database is a no-op, not a rebuild. That same
# no-op is why a column added to a table here has to be added to
# ADDED_COLUMNS below as well — an existing table is never re-created, so
# nothing else would ever give it the column.
#
# `trigger` and `action` are SQLite keywords, so those two column names are
# quoted; they are the contract's names and renaming them to dodge the
# quoting would break the mirror.
SCHEMA = """
-- projects: a repo + its autonomy policy (state-model §2).
CREATE TABLE IF NOT EXISTS projects (
    id                  INTEGER PRIMARY KEY,
    linearTeamId        TEXT    NOT NULL UNIQUE,  -- maps 1:1 to a Linear team
    repoPath            TEXT    NOT NULL,
    defaultBranch       TEXT    NOT NULL,
    autonomyProfile     TEXT    NOT NULL
        CHECK (autonomyProfile IN ('personal', 'shared_low_risk', 'production')),
    highRiskPaths       TEXT    NOT NULL DEFAULT '[]',  -- JSON string[] of globs
    verificationDefault TEXT,
    -- Epoch ms the supervisor's board fallback last asked Linear for the
    -- ready listing, so `board_ask_sec` throttles across passes and across
    -- the supervisor's restarts. NULL until the first ask.
    boardAskedAt        INTEGER,
    launchBackoffUntil  INTEGER,
    launchBackoffReason TEXT,  -- JSON: reason, since, interval (seconds)
    -- §7: the per-project single-threading lease. Held here rather than
    -- inferred from runs so a concurrent claim loses on a uniqueness-style
    -- assertion instead of on a race-prone count.
    activeRunId         INTEGER
        REFERENCES runs (id) DEFERRABLE INITIALLY DEFERRED
);

-- tickets: Holophyte's mirror of a Linear issue + loop-owned planning
-- fields (state-model §2). Status enum is §3.
CREATE TABLE IF NOT EXISTS tickets (
    id                   INTEGER PRIMARY KEY,
    projectId            INTEGER NOT NULL REFERENCES projects (id),
    linearIssueId        TEXT    NOT NULL UNIQUE,
    url                  TEXT,
    boardState           TEXT,
    linearIdentifier     TEXT    NOT NULL,  -- e.g. "HOL-142", for humans
    title                TEXT    NOT NULL,
    -- The Linear body the loop last read at claim time, so what the daemon
    -- serves is the exact contract the run worked from (KO-328). Empty,
    -- not NULL, for a row mirrored before the column existed.
    body                 TEXT    NOT NULL DEFAULT '',
    status               TEXT    NOT NULL
        CHECK (status IN ('needs_spec', 'ready', 'in_flight', 'blocked_on_deps',
                          'blocked_on_operator', 'merged', 'abandoned')),
    -- Empty either list makes the ticket unpickable by §2's predicate, which
    -- is why they default to '[]' rather than to NULL: "not specced yet" and
    -- "specced with nothing in it" are the same unpickable state.
    acceptanceCriteria   TEXT    NOT NULL DEFAULT '[]',  -- JSON string[]
    verificationCommands TEXT    NOT NULL DEFAULT '[]',  -- JSON string[]
    timeBoxMs            INTEGER,                        -- from the Linear estimate
    affinity             TEXT    NOT NULL
        CHECK (affinity IN ('any', 'gui', 'headless')),
    dependsOn            TEXT    NOT NULL DEFAULT '[]',  -- JSON linearIssueId[]
    activeRunId          INTEGER
        REFERENCES runs (id) DEFERRABLE INITIALLY DEFERRED,
    lastRunId            INTEGER
        REFERENCES runs (id) DEFERRABLE INITIALLY DEFERRED,
    blockedQuestion      TEXT,                           -- set when blocked_on_operator
    splitDepth           INTEGER NOT NULL DEFAULT 0,     -- 0 = original ticket
    mirroredAt           INTEGER NOT NULL
);

-- runs: one attempt at one ticket (state-model §2). Phase enum is §4.
CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY,
    ticketId          INTEGER NOT NULL REFERENCES tickets (id),
    projectId         INTEGER NOT NULL REFERENCES projects (id),
    attempt           INTEGER NOT NULL,  -- 1-based
    phase             TEXT    NOT NULL
        CHECK (phase IN ('claimed', 'working', 'verifying', 'reviewing',
                         'addressing', 'merge_gate', 'awaiting_merge_approval',
                         'merging', 'squashing', 'done', 'blocked_on_operator',
                         'failed', 'killed', 'rejected')),
    workerId          TEXT,
    providerSessionId TEXT,
    branch            TEXT,
    prUrl             TEXT,
    startedAt         INTEGER NOT NULL,
    lastHeartbeat     INTEGER NOT NULL,  -- staleness detection
    endedAt           INTEGER,
    reviewRoundCount  INTEGER NOT NULL DEFAULT 0,
    -- The ticket's time box as it stood when this run was claimed. Not a
    -- second copy of `tickets.timeBoxMs` for its own sake: the ticket's
    -- estimate can be re-pointed by any later mirror, and an estimate that
    -- moves after the fact makes every estimate-vs-actual reading of a
    -- finished run change with it. The run's snapshot is what it was actually
    -- given.
    timeBoxMs         INTEGER,
    workingMs         INTEGER, -- NULL means historical/unmeasured
    workStartedAt     INTEGER, -- epoch milliseconds of the active work call
    -- The ticket's contract as it stood at the claim: its title and the two
    -- lists §2's pickability predicate reads, as one canonical JSON document
    -- (`contract_snapshot()` below). A run is worked to the ticket it was
    -- claimed under, so the freeze is what the merge gate holds the live
    -- ticket against -- a body edited mid-run is then caught before the
    -- branch lands rather than after. NULL is "no snapshot taken" (a run
    -- claimed by a module older than this column), which the drift check
    -- reads as nothing to compare rather than as no drift.
    ticketSnapshot    TEXT,
    outcome           TEXT
        CHECK (outcome IS NULL
               OR outcome IN ('merged', 'killed', 'abandoned', 'failed', 'rejected')),
    outcomeReason     TEXT,
    -- The merge commit a `merged` run landed on main as, the full sha.
    -- NULL until the merge close-out writes it, and NULL forever on a run
    -- that ended any other way or was released by a module older than the
    -- column: FINDINGS renders the entry without a sha in either case.
    mergeSha          TEXT,
    -- Whether a failure says anything about the ticket. `work` is the
    -- default and the ordinary case: the run got as far as the work and the
    -- work is what failed. `infra` is a run that ended before any work
    -- started (a claim race) or because the factory's own plumbing gave out
    -- (a reviewer container that would not start): true about the factory,
    -- silent about the ticket, and so left out of the escalation count that
    -- parks a ticket for a human. The report still shows both.
    outcomeClass      TEXT    NOT NULL DEFAULT 'work'
        CHECK (outcomeClass IN ('work', 'infra')),
    -- The hostname that claimed the run. A target is pinned to one host
    -- and its store may be read from another, so the row is the only
    -- place "where is this run executing" can be answered from. Nullable:
    -- rows older than the column are not backfilled.
    host              TEXT,
    -- §5's "re-enters the phase it left": the phase a parked run goes back
    -- to, written by whoever parks it and consumed by `resume()`. Not a
    -- state-model field — the doc states the rule and leaves the mechanism
    -- open, and a column is cheaper to keep true than reconstructing the
    -- phase from the runEvents log. NULL means "nothing recorded", which
    -- `resume()` reads as §4's drawn edge back, `working`.
    resumePhase       TEXT
        CHECK (resumePhase IS NULL
               OR resumePhase IN ('claimed', 'working', 'verifying', 'reviewing',
                                  'addressing', 'merge_gate',
                                  'awaiting_merge_approval', 'merging',
                                  'squashing', 'done', 'blocked_on_operator',
                                  'failed', 'killed', 'rejected')),
    -- The candidate a run parked awaiting merge approval was parked on: the
    -- full sha the reviewer approved and the pre-merge verify passed.
    -- Written by `park()` and read by the loop's resume at the merge gate,
    -- which merges that sha and nothing else -- a worktree that has moved
    -- on since is not what the operator approved. NULL on every run that
    -- was never parked there.
    candidateSha      TEXT,
    -- The candidate the last independent judgement covered: the reviewer's
    -- approval, or the operator's `--approve`. Written by `park()` under
    -- `[merge] mode = "pr"` beside `candidateSha`, which a fix round or a
    -- rejected fix can move past it; read by the babysitter a `--babysit`
    -- resumes, which reviews a candidate at any other sha again before
    -- the merge API is called rather than merging on the branch's word.
    -- NULL on every run parked with no judgement to record.
    approvedSha       TEXT,
    -- Explicit operator consent; babysit and requeue clear both (KO-513).
    approvedAt        INTEGER,
    approvedBy        TEXT,
    -- The review-round cap the loop gave this run: its `[loop]` review
    -- keys applied to the candidate's size, measured once before round 1
    -- (KO-299). Written by `set_review_round_cap()` where the loop computes
    -- it and read by `/runs/N` as `max_rounds`, so the console sizes the
    -- round timeline by the cap this run had rather than a constant
    -- (KO-321). NULL on every run recorded before the column existed.
    reviewRoundCap    INTEGER,
    -- What the last babysitter pass saw of the pull request a run is parked
    -- on, recorded after the pass's own pushes and replies (KO-362):
    -- GitHub's `updatedAt` as the ISO 8601 string it answers, and the
    -- count of review threads. The loop's per-tick reconcile holds the
    -- pull request's current values against these, and a newer
    -- `updatedAt` or a grown count is review activity the babysitter has
    -- not answered. NULL on a run parked with no pull request, by a
    -- module older than the columns, or when GitHub could not be asked
    -- at the park, which the reconcile reads as "record, do not
    -- shepherd".
    prSeenAt          TEXT,
    prSeenThreads     INTEGER,
    prSeenChecks      TEXT,
    prSeenReview      TEXT,
    UNIQUE (ticketId, attempt)
);

-- reviewRounds: one bot review pass within a run (state-model §2).
CREATE TABLE IF NOT EXISTS reviewRounds (
    id                  INTEGER PRIMARY KEY,
    runId               INTEGER NOT NULL REFERENCES runs (id),
    round               INTEGER NOT NULL,  -- 1-based within the run
    -- JSON { command, exitCode, output }[]
    verificationResults TEXT    NOT NULL DEFAULT '[]',
    verdict             TEXT    NOT NULL
        CHECK (verdict IN ('pass', 'changes_requested', 'error')),
    -- JSON { path, line?, severity, criterion?, message }[]
    findings            TEXT    NOT NULL DEFAULT '[]',
    findingsFingerprint TEXT    NOT NULL,  -- hash of sorted (path:line:severity)
    reviewerModel       TEXT    NOT NULL,
    startedAt           INTEGER NOT NULL,
    endedAt             INTEGER,
    UNIQUE (runId, round)
);

-- runEvents: append-only log, one stream per run (state-model §2).
CREATE TABLE IF NOT EXISTS runEvents (
    id      INTEGER PRIMARY KEY,
    runId   INTEGER REFERENCES runs (id),
    projectId INTEGER REFERENCES projects (id),
    seq     INTEGER NOT NULL,  -- monotonic per run
    level   TEXT    NOT NULL CHECK (level IN ('narrative', 'detail')),
    kind    TEXT    NOT NULL,  -- 'phase_change' | 'tool_use' | 'supervisor_probe' | ...
    summary TEXT    NOT NULL,  -- human-readable, always present
    payload TEXT,              -- JSON, detail level only
    at      INTEGER NOT NULL,
    CHECK (runId IS NOT NULL OR projectId IS NOT NULL),
    UNIQUE (runId, seq)
);

-- sweepStrikes: the supervisor sweep's per-run liveness tally. Not a
-- state-model table: a single stale-heartbeat sample false-positives on a
-- load spike (v1 TUI mining), so a run must be seen silent by two
-- consecutive sweeps before it trips -- and "consecutive" needs somewhere to
-- live between two separate sweep invocations, which are separate processes.
-- One row per run currently under suspicion; a run seen alive has its row
-- dropped, and a run whose heartbeat is newer than the strike on file starts
-- over at one, so the count is consecutive in silence rather than in sweeps.
CREATE TABLE IF NOT EXISTS sweepStrikes (
    runId    INTEGER PRIMARY KEY REFERENCES runs (id),
    strikes  INTEGER NOT NULL,
    lastSeen INTEGER NOT NULL  -- when the latest strike was recorded, so a
                               -- heartbeat newer than it restarts the count
);

-- supervisorHeartbeats: one row per supervisor process (`--supervise`),
-- bumped on every pass it makes. Not a state-model table either: it exists
-- so a reader of the store -- `--report`, a dashboard, an operator wondering
-- whether the overnight watcher is still watching -- can tell a supervisor
-- that is alive from one that died, the same question the sweep asks of a
-- run. Keyed by the process, not the pass: a row per pass would grow by the
-- minute and answer nothing a row per process does not.
CREATE TABLE IF NOT EXISTS supervisorHeartbeats (
    pid       INTEGER NOT NULL,
    startedAt INTEGER NOT NULL,  -- when this supervisor process took the lock
    lastBeat  INTEGER NOT NULL,  -- when it last completed a pass
    passes    INTEGER NOT NULL,  -- how many passes it has completed
    host      TEXT,              -- the machine the supervisor runs on
    PRIMARY KEY (pid, startedAt)
);

-- loopRestarts: one row per self-merge re-exec of the loop, written just
-- before the exec replaces the process. The shape of `supervisorHeartbeats`
-- turned around: the heartbeat says "the watcher is still here", this says
-- "the loop is about to leave and means to come back" -- and the sweep is the
-- witness for whether it did. A loop that came back claims (a `runs` row with
-- a heartbeat newer than `at`) or writes its exit note (`returnedAt`); one
-- that died in the exec does neither, and past the grace window the sweep
-- prints and stamps `reportedAt`, once, so the same silence is not reported
-- on every pass.
CREATE TABLE IF NOT EXISTS loopRestarts (
    id         INTEGER PRIMARY KEY,
    projectId  INTEGER NOT NULL REFERENCES projects (id),
    sha        TEXT    NOT NULL,  -- the merged commit the loop re-executed from
    at         INTEGER NOT NULL,  -- when the exec was about to happen
    returnedAt INTEGER,           -- when a loop next wrote its exit note
    reportedAt INTEGER            -- when the sweep reported it unreturned
);

-- linearDeliveries: webhook idempotency (state-model §1). The delivery id is
-- the primary key, so a replayed delivery collides instead of re-running its
-- effect.
CREATE TABLE IF NOT EXISTS linearDeliveries (
    deliveryId  TEXT    PRIMARY KEY,
    processedAt INTEGER NOT NULL
);

-- ledger: the narrative of a run, one row per entry (design note 9). What
-- the loop used to say only as a board comment -- a round's verdict and the
-- implementer's answer, the merge line, a failure's why, an operator's
-- intervention -- lands here first, and the board comment is its projection.
-- Distinct from runEvents, which is the phase machine's own stream: a ledger
-- row is prose written for a reader, an event is a state change. `kind`
-- says which shape of prose; `source` says who wrote it.
CREATE TABLE IF NOT EXISTS ledger (
    id       INTEGER PRIMARY KEY,
    runId    INTEGER NOT NULL REFERENCES runs (id),
    ticketId INTEGER NOT NULL REFERENCES tickets (id),
    at       INTEGER NOT NULL,
    kind     TEXT    NOT NULL
        CHECK (kind IN ('merge', 'failure', 'round', 'adjudication',
                        'intervention', 'note')),
    text     TEXT    NOT NULL,
    source   TEXT    NOT NULL CHECK (source IN ('loop', 'operator'))
);
"""

# interventions: supervisor/human actions on a run (state-model §2). Kept out
# of runEvents because these are queryable decisions, not log lines. Defined
# outside SCHEMA because `_widen_interventions_action()` below rebuilds an
# older store's table from this exact DDL — a rebuild transcribed by hand
# could drift from the schema, and then a migrated store and a fresh one
# would disagree about what the table accepts.
_INTERVENTIONS_DDL = """
CREATE TABLE IF NOT EXISTS interventions (
    id        INTEGER PRIMARY KEY,
    runId     INTEGER REFERENCES runs (id),
    projectId INTEGER REFERENCES projects (id),
    source    TEXT    NOT NULL CHECK (source IN ('supervisor', 'human', 'factory')),
    "trigger" TEXT    NOT NULL
        CHECK ("trigger" IN ('time_box', 'off_criteria', 'looping',
                             'review_stuck', 'linear_cancelled',
                             'linear_completed', 'manual')),
    "action"  TEXT    NOT NULL
        CHECK ("action" IN ('redirect', 'kill', 'extend_time_box', 'resume',
                            'close_out', 'requeue', 'approve', 'repoint',
                            'babysit', 'reconcile', 'restart_supervisor',
                            'launch_loop', 'launch_backoff', 'route_fallback',
                            'config_edit', 'operator_note', 'migrate')),
    note      TEXT,  -- store-level migration evidence as JSON
    question  TEXT,  -- for redirect
    guidance  TEXT,  -- human answer, only when the run was blocked_on_operator
    at        INTEGER NOT NULL,
    CHECK (runId IS NOT NULL OR projectId IS NOT NULL OR "action" = 'migrate')
)"""


# Version 23 mirrors Linear issue URLs for console ticket links (KO-478).
# Version 24 records the process responsible for schema migrations (KO-495).
# Version 25 mirrors board state names for attention and requeue (KO-503).
# Version 26 records explicit human merge approval on runs (KO-513).
SCHEMA_VERSION = 26

# How long a connection waits for another writer's lock before raising
# `database is locked`. WAL admits one writer at a time, and the loop's
# heartbeat thread, its phase changes and the supervisor's sweep are three
# writers on one file; the sqlite3 default of five seconds is shorter than a
# sweep under load, and run 103 (KO-273) died at a phase change on exactly
# that timing. Both `open()` and `store.read.open_readonly()` open with this
# value so the two agree. A lock held past it still raises; nothing here
# masks a real deadlock. Patch it below a second to witness the bound.
BUSY_TIMEOUT_S = 30

# Index hot foreign-key joins and per-run ledger reads. Idempotent DDL,
# applied after table creation only when open() permits migration.
INDEXES = """
CREATE INDEX IF NOT EXISTS runs_ticketId ON runs (ticketId);
CREATE INDEX IF NOT EXISTS reviewRounds_runId ON reviewRounds (runId);
CREATE INDEX IF NOT EXISTS runEvents_runId ON runEvents (runId);
CREATE INDEX IF NOT EXISTS ledger_runId ON ledger (runId);
"""


class SchemaNewer(SystemExit):
    """A newer factory migrated this store; an old loop must re-execute."""

    def __init__(self, path, version):
        self.version = version
        super().__init__(
            f"{path}: store schema version {version} is newer than the"
            f" version {SCHEMA_VERSION} this build understands; refusing"
            " to open it with an older factory")


class SchemaOlder(SystemExit):
    """A read-only command needs the store's lifecycle owner to migrate it."""

    def __init__(self, path, version, expected):
        self.version = version
        super().__init__(
            f"store at {path} is schema {version}; this build expects {expected};"
            " start the loop or the serve daemon to migrate it, or run the"
            " command from the build that wrote it")


class _Connection(sqlite3.Connection):
    """Allow a fallback heartbeat, serialized with the caller's transactions."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()


def _connect_with_version(path):
    """Retry only the initial WAL lock acquisition, never migration writes."""
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


def open(path, *, migrate=True):  # noqa: A001 - the ticket names this entry point open()
    """Open the store at `path` in WAL mode and return the connection.

    Refuse a newer `user_version` with `SchemaNewer` (a `SystemExit`) before
    writing. Migrate older stores with `init()` and create missing indexes.
    With `migrate=False`, refuse older stores with `SchemaOlder` and skip
    index creation. Require WAL so supervisor reads can overlap loop writes;
    a filesystem that cannot enable it raises rather than silently degrading."""
    # Before anything that writes, including the WAL switch below: a store a
    # newer module stamped is refused without touching it, so the file is
    # still exactly what that newer build left for it to reopen.
    conn, version = _connect_with_version(path)
    if version > SCHEMA_VERSION:
        conn.close()
        raise SchemaNewer(path, version)
    # Referential integrity is off by default in SQLite and is per-connection,
    # so it has to be asserted on every open, not once at init().
    conn.execute("PRAGMA foreign_keys = ON")
    # The connect() timeout again, as the pragma: it is the value a
    # `BEGIN IMMEDIATE` waits for on the write lock, and stating it on the
    # connection keeps it from depending on how sqlite3 applied the argument.
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_S * 1000}")
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if mode.lower() != "wal":
        conn.close()
        raise sqlite3.DatabaseError(
            f"{path}: could not enable WAL mode (journal_mode is {mode!r})"
        )
    try:
        if not migrate:
            if version < SCHEMA_VERSION:
                raise SchemaOlder(path, version, SCHEMA_VERSION)
            return conn
        if version < SCHEMA_VERSION:
            # 0 is every store made before the stamp existed, and a fresh
            # file; either way the ladder in init() carries it to the
            # current version and stamps it there, in one transaction.
            init(conn)
        conn.executescript(INDEXES)
    except BaseException:
        conn.close()
        raise
    return conn


# Additive migrations: (table, column, DDL). CREATE TABLE IF NOT EXISTS
# cannot extend old tables, so every new SCHEMA column needs an entry here.
# Historical rows retain NULL for nullable columns without a default.
#
# Each DDL is transcribed from that column's clause in SCHEMA so a migrated
# database and a fresh one end up with the same column, CHECK included:
# ALTER TABLE preserves CHECK; UNIQUE and NOT NULL without a default require
# rebuilding. The schema test compares migrated and fresh databases.
ADDED_COLUMNS = (
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
    (
        "runs",
        "timeBoxMs",
        "timeBoxMs INTEGER",
    ),
    (
        "runs",
        "resumePhase",
        "resumePhase TEXT"
        " CHECK (resumePhase IS NULL"
        " OR resumePhase IN ('claimed', 'working', 'verifying', 'reviewing',"
        "                    'addressing', 'merge_gate',"
        "                    'awaiting_merge_approval', 'merging', 'squashing',"
        "                    'done', 'blocked_on_operator', 'failed',"
        "                    'killed'))",
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
        " CHECK (outcomeClass IN ('work', 'infra'))",
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
        "projects",
        "boardAskedAt",
        "boardAskedAt INTEGER",
    ),
)


# Rows an older version of this module wrote without a value, as (what it
# repairs, SQL). `ADDED_COLUMNS` carries a store forward far enough to be read
# from; this carries it forward far enough to be read *correctly*, which is a
# different problem: `runs.reviewRoundCount` has shipped since the first
# schema, but nothing wrote it until close-out started stamping it, so every
# run that ended before then still holds the column's `DEFAULT 0` while its
# `reviewRounds` rows say otherwise. The report and FINDINGS.md read the
# column, so left alone those runs would each claim zero rounds -- not a
# missing reading but a confidently wrong one.
#
# Each statement is written to be self-limiting: it selects only the rows that
# disagree with the truth it recomputes, so the second call matches nothing
# and `init()` stays idempotent. Only ended runs are touched, because a run
# still in flight has not reached the close-out that owns this column and its
# count is not final yet.
BACKFILLS = (
    (
        "runs.reviewRoundCount on runs that ended before close-out stamped it",
        "UPDATE runs SET reviewRoundCount ="
        "     (SELECT COUNT(*) FROM reviewRounds WHERE runId = runs.id)"
        " WHERE endedAt IS NOT NULL"
        "   AND reviewRoundCount <>"
        "     (SELECT COUNT(*) FROM reviewRounds WHERE runId = runs.id)",
    ),
)


def init(conn):
    """Create every table the state model defines, if absent, and migrate.

    Three steps, because `CREATE TABLE IF NOT EXISTS` alone would only ever
    bootstrap an empty file: the tables are created, then every `ADDED_COLUMNS`
    entry missing from an existing table is added, then every `BACKFILLS`
    statement repairs the rows an older version of this module left with a
    value it never filled in. The second step is what carries a store created
    by an earlier version forward instead of leaving it one column short of
    the code that reads it; the third is what keeps that store's history from
    reading as a confident zero.

    Idempotent: safe to call on an already-initialized database, where it
    creates nothing, adds nothing, and touches only rows a backfill finds
    still disagreeing with what it recomputes — none, on the second call, and
    none ever on a store this module wrote from the start. Not a downgrade
    path — an older module opening a newer store sees columns it does not know
    about, which is harmless, while the reverse is what this repairs.
    """
    conn.executescript(SCHEMA)
    foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    # Everything after the executescript rolls back together on failure: a
    # migration that died must not leave an open transaction holding its
    # half-done work, because the next caller's `executescript` would issue
    # an implicit COMMIT and make the half-state durable — the exact hazard
    # `_transaction()`'s docstring warns joined writers about.
    try:
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute(_INTERVENTIONS_DDL)
        for table, column, ddl in ADDED_COLUMNS:
            # A misspelled table name leaves `columns` empty and the ALTER
            # then raises `no such table`, the loud failure this should be.
            columns = {row[1]
                       for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        # After the ALTERs, not before: a backfill is free to read a column
        # the step above has only just added.
        for _repairs, sql in BACKFILLS:
            conn.execute(sql)
        _widen_runs_outcomes(conn)
        _widen_interventions_action(conn)
        _project_startup_events(conn)
        # Stamped last and inside the same transaction as the ladder, so a
        # store carries the version only once it holds everything the
        # version means.
        if version < SCHEMA_VERSION:
            _record_migration(conn, version)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
        if conn.execute("PRAGMA foreign_key_check").fetchall() != foreign_key_errors:
            raise sqlite3.IntegrityError("foreign key violation during migration")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute(f"PRAGMA foreign_keys = {foreign_keys}")


def _widen_runs_outcomes(conn):
    """Rebuild the run CHECKs for rejected outcomes (KO-431)."""
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
    """Identify the running code and process inside the migration transaction."""
    try:
        build = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        build = "unknown"
    at = int(time.time() * 1000)
    note = json.dumps({"from": version, "to": SCHEMA_VERSION, "build": build,
                       "pid": os.getpid(), "ppid": os.getppid(),
                       "argv": sys.argv, "user": getpass.getuser(), "at": at})
    conn.execute(
        'INSERT INTO interventions (source, "trigger", action, note, at)'
        " VALUES ('factory', 'manual', 'migrate', ?, ?)", (note, at))


def _widen_interventions_action(conn):
    """Rebuild `interventions` when its action CHECK predates 'repoint',
    'shepherd', 'reconcile', the daemon's unit actions, 'config_edit' or
    the rename of 'shepherd' to 'babysit'."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table'"
        " AND name = 'interventions'").fetchone()
    if row is None:
        return  # nothing to widen; the DDL step in init() creates it
    # Scoped to the action CHECK's own value list, not the whole DDL: the
    # literal appearing anywhere else (a future comment, a default) must not
    # skip a rebuild that is still needed.
    (ddl,) = row
    admitted = ddl.partition('"action" IN (')[2].partition(")")[0]
    if all(value in admitted
           for value in ("'repoint'", "'babysit'", "'reconcile'", "'operator_note'",
                         "'restart_supervisor'", "'launch_loop'",
                         "'config_edit'", "'launch_backoff'", "'route_fallback'",
                         "'migrate'")):
        return
    # The copy runs with foreign keys enforced, so an orphaned row — a
    # `runId` no run has, the kind a raw-SQL session with FKs off leaves —
    # would abort the rebuild at the INSERT. Refusing up front instead names
    # the problem and the fix, and means the copy below cannot half-fail.
    (orphans,) = conn.execute(
        "SELECT COUNT(*) FROM interventions i LEFT JOIN runs r"
        " ON r.id = i.runId WHERE r.id IS NULL AND i.runId IS NOT NULL").fetchone()
    if orphans:
        raise sqlite3.IntegrityError(
            f"{orphans} interventions row(s) reference runs that do not"
            " exist; repair them before this store can migrate")
    with _transaction(conn):
        conn.execute("ALTER TABLE interventions RENAME TO interventions_old")
        conn.execute(_INTERVENTIONS_DDL)
        conn.execute(
            "INSERT INTO interventions"
            ' (id, runId, source, "trigger", "action", question, guidance, at,'
            ' projectId, note)'
            ' SELECT id, runId, source, "trigger",'
            "   CASE \"action\" WHEN 'shepherd' THEN 'babysit'"
            '   ELSE "action" END, question, guidance, at, projectId, note'
            " FROM interventions_old")
        conn.execute("DROP TABLE interventions_old")


@contextlib.contextmanager
def _transaction(conn):
    """Run the block in one `BEGIN IMMEDIATE`, or join the caller's transaction.

    `BEGIN IMMEDIATE` serializes read-then-write operations across connections.
    The connection lock serializes fallback heartbeats with their caller:
    another thread must not join the caller's open transaction.
    Commit on success; roll back on any exception, including commit failure.

    Nested calls join the owning thread's transaction without committing or
    rolling it back. The owner must take IMMEDIATE for serialization, as
    `transaction()` and `claim()` do.
    """
    with getattr(conn, "_lock", contextlib.nullcontext()):
        if conn.in_transaction:
            yield
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            # Inside the guard: a deferred constraint the block violated is only
            # checked at COMMIT, and SQLite leaves the
            # transaction *open* when it fails that way. Unrolled back, the block's
            # writes stay pending on the connection, and the next `_transaction()`
            # would see `in_transaction` and silently join that contaminated state
            # instead of starting clean.
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


@contextlib.contextmanager
def transaction(conn):
    """`_transaction()` for callers outside this module; same guarantees.

    A reader that has to *decide* something from what it reads and then write
    the decision down cannot do the two in separate statements: between them
    another connection commits, and the write records a verdict on a state
    that no longer holds. The supervisor sweep is exactly that shape -- it
    reads a run's heartbeat, classifies it and records the sighting -- and it
    lives in `factory.py`, so the module's own writers' `BEGIN IMMEDIATE`
    needs a name that is not private to reach it.

    Under it, this module's writers join instead of opening their own, so a
    block may read, classify and call `record_strike()` (or any other writer)
    and have the whole thing commit or roll back once. The write lock is held
    from the first statement, so a concurrent writer waits rather than
    interleaving -- keep the block short for that reason.
    """
    with _transaction(conn):
        yield


def _project_startup_events(conn):
    """Allow startup evidence before any run has claimed a ticket (KO-466)."""
    columns = conn.execute("PRAGMA table_info(runEvents)").fetchall()
    if not any(row[1] == "runId" and row[3] for row in columns):
        return
    conn.execute("ALTER TABLE runEvents RENAME TO runEvents_old")
    ddl = SCHEMA.split("CREATE TABLE IF NOT EXISTS runEvents (", 1)[1].split(
        ");", 1)[0]
    conn.execute("CREATE TABLE runEvents (" + ddl + ")")
    conn.execute(
        "INSERT INTO runEvents (id, runId, seq, level, kind, summary, payload, at,"
        " projectId) SELECT id, runId, seq, level, kind, summary, payload, at,"
        " projectId FROM runEvents_old")
    conn.execute("DROP TABLE runEvents_old")
