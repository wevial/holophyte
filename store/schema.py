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

from . import enums as _enums

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
SCHEMA = f"""
-- projects: a repo + its autonomy policy (state-model §2).
CREATE TABLE IF NOT EXISTS projects (
    id                  INTEGER PRIMARY KEY,
    linearTeamId        TEXT    NOT NULL UNIQUE,  -- maps 1:1 to a Linear team
    repoPath            TEXT    NOT NULL,
    defaultBranch       TEXT    NOT NULL,
    autonomyProfile     TEXT    NOT NULL
        {_enums.check_clause('autonomyProfile', _enums.AutonomyProfile)},
    highRiskPaths       TEXT    NOT NULL DEFAULT '[]',  -- JSON string[] of globs
    verificationDefault TEXT,
    admission           TEXT NOT NULL DEFAULT 'enabled'
        {_enums.check_clause('admission', _enums.ProjectAdmission)},
    holdNote            TEXT,
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
        {_enums.check_clause('status', _enums.TicketStatus)},
    -- Empty either list makes the ticket unpickable by §2's predicate, which
    -- is why they default to '[]' rather than to NULL: "not specced yet" and
    -- "specced with nothing in it" are the same unpickable state.
    acceptanceCriteria   TEXT    NOT NULL DEFAULT '[]',  -- JSON string[]
    verificationCommands TEXT    NOT NULL DEFAULT '[]',  -- JSON string[]
    timeBoxMs            INTEGER,                        -- from the Linear estimate
    affinity             TEXT    NOT NULL
        {_enums.check_clause('affinity', _enums.Affinity)},
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
        {_enums.check_clause('phase', _enums.RunPhase)},
    workerId          TEXT,
    providerSessionId TEXT,
    branch            TEXT,
    prUrl             TEXT,
    parkKind          TEXT {_enums.check_clause("parkKind", _enums.ParkKind)},
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
    -- The verify part of `workingMs` (KO-635): the time box judges agent
    -- work, `workingMs - verifyMs`. NULL for a run claimed before the column.
    verifyMs          INTEGER,
    verifyStartedAt   INTEGER, -- set with workStartedAt when the span is a verify
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
        {_enums.check_clause('outcome', _enums.RunOutcome)},
    outcomeReason     TEXT,
    failureKind TEXT
        {_enums.check_clause('failureKind', _enums.FailureKind)},
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
        {_enums.check_clause('outcomeClass', _enums.OutcomeClass)},
    -- The hostname that claimed the run. A target is pinned to one host
    -- and its store may be read from another, so the row is the only
    -- place "where is this run executing" can be answered from. Nullable:
    -- rows older than the column are not backfilled.
    host              TEXT,
    -- The pid of the process that claimed the run and works it on `host`,
    -- so `--abort` can tell a dead worker from a live one (KO-592).
    workerPid         INTEGER,
    -- §5's "re-enters the phase it left": the phase a parked run goes back
    -- to, written by whoever parks it and consumed by `resume()`. Not a
    -- state-model field — the doc states the rule and leaves the mechanism
    -- open, and a column is cheaper to keep true than reconstructing the
    -- phase from the runEvents log. NULL means "nothing recorded", which
    -- `resume()` reads as §4's drawn edge back, `working`.
    stopRequested     INTEGER REFERENCES interventions(id),
    resumePhase       TEXT
        {_enums.check_clause('resumePhase', _enums.ResumePhase)},
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
    -- The pull request's title as the same read saw it, so `/attention`'s
    -- `pr_open` item names the pull request and not only its number
    -- (KO-622). NULL until a read recorded one.
    prSeenTitle       TEXT,
    UNIQUE (ticketId, attempt)
);

-- reviewRounds: one bot review pass within a run (state-model §2).
CREATE TABLE IF NOT EXISTS reviewRounds (
    id                  INTEGER PRIMARY KEY,
    runId               INTEGER NOT NULL REFERENCES runs (id),
    round               INTEGER NOT NULL,  -- 1-based within the run
    -- JSON {{ command, exitCode, output }}[]
    verificationResults TEXT    NOT NULL DEFAULT '[]',
    verdict             TEXT    NOT NULL
        {_enums.check_clause('verdict', _enums.ReviewVerdict)},
    -- JSON {{ path, line?, severity, criterion?, message }}[]
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
    level   TEXT    NOT NULL {_enums.check_clause('level', _enums.EventLevel)},
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
        {_enums.check_clause('kind', _enums.LedgerKind)},
    text     TEXT    NOT NULL,
    source   TEXT    NOT NULL {_enums.check_clause('source', _enums.LedgerSource)}
);
"""

# interventions: supervisor/human actions on a run (state-model §2). Kept out
# of runEvents because these are queryable decisions, not log lines. Defined
# outside SCHEMA because `_widen_interventions_action()` below rebuilds an
# older store's table from this exact DDL — a rebuild transcribed by hand
# could drift from the schema, and then a migrated store and a fresh one
# would disagree about what the table accepts.
_INTERVENTIONS_DDL = f"""
CREATE TABLE IF NOT EXISTS interventions (
    id        INTEGER PRIMARY KEY,
    runId     INTEGER REFERENCES runs (id),
    projectId INTEGER REFERENCES projects (id),
    source    TEXT    NOT NULL {_enums.check_clause('source',
                                                  _enums.InterventionSource)},
    "trigger" TEXT    NOT NULL
        {_enums.check_clause('trigger', _enums.InterventionTrigger)},
    "action"  TEXT    NOT NULL
        {_enums.check_clause('action', _enums.InterventionAction)},
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
# Version 27 generates every enum CHECK from store.enums (KO-579).
# Version 28 adds project admission holds and their interventions (KO-578).
# Version 29 records typed run failure kinds with prefix backfill (KO-584).
# Version 30 adds disabled project admission and registration (KO-586).
# Version 31 types run park reasons and backfills legacy questions (KO-583).
# Version 33 records the pull request title the reconcile read (KO-622).
# Version 34 records verify time apart from agent work on runs (KO-635).
# Version 35 admits the `abort_close` intervention action (KO-611).
# Version 36 admits the `not_reproduced` run park kind (KO-657).
SCHEMA_VERSION = 36

# The oldest SCHEMA_VERSION whose builds can still read and write a store at
# SCHEMA_VERSION (KO-661). Each migration records it in its `migrate` note,
# and a build behind the store opens it unmigrated when its own version is at
# or above the floor that note names. On each bump, keep it for an additive
# change and raise it to the new version for any other:
#
# * Additive: a new table or index; a new column that is nullable, or
#   NOT NULL with a DEFAULT (an older build's INSERTs name their columns);
#   a backfill that writes only columns the same bump adds.
# * Not additive: a dropped or renamed column or table; a new or tightened
#   CHECK, UNIQUE or NOT NULL on an existing column; an enum value removed
#   or renamed, since the newer CHECK rejects an older build's write.
# * An added enum value is additive only on a column an older build records
#   or displays and never branches on (`interventions.action`,
#   `runEvents.level`, `ledger.kind`, `runs.failureKind`). It is not on
#   `runs.phase` (bump 32's `paused` raises KeyError in an older
#   `set_phase()`), `projects.admission` (bump 30's `disabled` is claimed
#   on by a build that tests only for `held`), `tickets.status` (it decides
#   pickability) or `runs.parkKind` (the claim and the serve daemon choose a
#   path from it).
READABLE_FROM = SCHEMA_VERSION

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
    """A non-owner needs the supervisor to migrate the store."""

    def __init__(self, path, version, expected):
        self.version = version
        super().__init__(
            f"store at {path} is schema {version}; this build expects {expected};"
            " start the supervisor to migrate it, or run the command from the"
            " build that wrote it")


class SchemaError(sqlite3.DatabaseError):
    """The store's schema names a table it does not have (KO-664).

    A foreign key to a missing table fails every insert into its table, so
    the store is refused as a whole, naming each dangling key, instead of
    letting writes fail one by one."""

    def __init__(self, dangling):
        self.dangling = dangling
        super().__init__("store schema references missing tables: " + "; ".join(
            f"{table}.{column} references {parent}, which does not exist"
            for table, column, parent in dangling))


def _refuse_dangling_references(conn):
    """Raise `SchemaError` if any foreign key names a table that is absent."""
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


def latest_migration_note(conn):
    """The newest `migrate` note as JSON text, or None when there is none.

    A store whose `interventions` table has no `note` column, or no such
    table at all, has no migration history to read."""
    if "note" not in {r[1] for r in conn.execute("PRAGMA table_info(interventions)")}:
        return None
    row = conn.execute(
        "SELECT note FROM interventions WHERE action = 'migrate'"
        " AND note IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    return None if row is None else row[0]


def _readable_from(conn, version):
    """The floor the migration to `version` recorded, or None.

    A note that records another target version is not this version's
    floor: a stamp moved without its migration record has none."""
    note = latest_migration_note(conn)
    try:
        detail = json.loads(note) if note is not None else {}
    except ValueError:
        return None
    if not isinstance(detail, dict) or detail.get("to") != version:
        return None
    floor = detail.get("readableFrom")
    return floor if isinstance(floor, int) else None


def open(path, *, migrate=False, on_migrate=None):  # noqa: A001 - the ticket names this entry point open()
    """Open the store at `path` in WAL mode and return the connection.

    Refuse a newer `user_version` with `SchemaNewer` (a `SystemExit`) before
    writing, unless its migrate note's `readableFrom` floor is at or below
    this build's version; such a store is opened as it is, never migrated or
    indexed, so its stamp is never lowered. Only `migrate="owner"` may
    initialize or migrate the store and create missing indexes, passing
    `on_migrate` to `init()`; other callers refuse older stores with
    `SchemaOlder` before any writes. Refuse
    with `SchemaError` a store whose foreign keys name a missing table.
    Require WAL so supervisor reads can overlap loop writes; a filesystem
    that cannot enable it raises rather than silently degrading."""
    # Before anything that writes, including the WAL switch below: a store a
    # newer module stamped is refused without touching it, so the file is
    # still exactly what that newer build left for it to reopen.
    if migrate != "owner" and not Path(path).exists():
        raise SchemaOlder(path, 0, SCHEMA_VERSION)
    conn, version = _connect_with_version(path)
    newer = version > SCHEMA_VERSION
    if newer:
        floor = _readable_from(conn, version)
        if floor is None or floor > SCHEMA_VERSION:
            conn.close()
            raise SchemaNewer(path, version, floor)
    owner = migrate == "owner"
    if version < SCHEMA_VERSION and not owner:
        conn.close()
        raise SchemaOlder(path, version, SCHEMA_VERSION)
    # Referential integrity is off by default in SQLite and is per-connection,
    # so it has to be asserted on every open, not once at init().
    conn.execute("PRAGMA foreign_keys = ON")
    # The connect() timeout again, as the pragma: it is the value a
    # `BEGIN IMMEDIATE` waits for on the write lock, and stating it on the
    # connection keeps it from depending on how sqlite3 applied the argument.
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_S * 1000}")
    try:
        if owner and version < SCHEMA_VERSION:
            # 0 is every store made before the stamp existed, and a fresh
            # file; either way the ladder in init() carries it to the
            # current version and stamps it there, in one transaction.
            # init() refuses a dangling key before it commits, so a store
            # this refuses is left as it was found.
            init(conn, on_migrate)
        # After migrating, not before: an older store may reference a table
        # only the ladder creates. Read-only, ahead of the WAL switch and the
        # index writes, both of which persist.
        _refuse_dangling_references(conn)
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode.lower() != "wal":
            raise sqlite3.DatabaseError(
                f"{path}: could not enable WAL mode (journal_mode is {mode!r})")
        if owner and not newer:
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
    ("runs", "timeBoxMs", "timeBoxMs INTEGER"),
    (
        "runs",
        "resumePhase",
        "resumePhase TEXT " + _enums.check_clause("resumePhase", _enums.ResumePhase),
    ),
    ("runs", "ticketSnapshot", "ticketSnapshot TEXT"),
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
        "     (SELECT COUNT(*) FROM reviewRounds"
        "      WHERE runId = runs.id AND verdict != 'error')"
        " WHERE endedAt IS NOT NULL"
        "   AND reviewRoundCount <>"
        "     (SELECT COUNT(*) FROM reviewRounds"
        "      WHERE runId = runs.id AND verdict != 'error')",
    ),
)


def init(conn, on_migrate=None):
    """Create every table the state model defines, if absent, and migrate.

    Add missing columns, repair historical rows, and rebuild constrained
    tables inside one transaction with foreign keys checked before commit.
    A migration calls `on_migrate(conn, from_version)` in that transaction,
    so its caller's record commits or rolls back with the stamp. Repeated
    initialization preserves existing rows and the schema version.
    """
    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    # Everything rolls back together on failure, the tables SCHEMA creates
    # included: a migration that died must not leave an open transaction
    # holding its half-done work, because the next caller's `executescript`
    # would issue an implicit COMMIT and make the half-state durable — the
    # exact hazard `_transaction()`'s docstring warns joined writers about.
    # The BEGIN opens the script because `executescript` commits whatever is
    # pending before it runs, and would otherwise run SCHEMA in autocommit.
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
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
        # Every CHECK is generated from store.enums: rebuilt at 32 for the
        # `paused` phase, and at 36 for the `not_reproduced` park (KO-657).
        if version < 36:
            _rebuild_enum_tables(conn)
        # Stamped last and inside the same transaction as the ladder, so a
        # store carries the version only once it holds everything the
        # version means.
        if version < SCHEMA_VERSION:
            _record_migration(conn, version)
            if on_migrate is not None:
                on_migrate(conn, version)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
        # A migration that left a key naming a missing table rolls back here
        # rather than committing a store that refuses its own writes.
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
    """Copy constrained tables through the generated DDL in init's transaction."""
    for table in dict.fromkeys(table for table, _ in _enums.CONSTRAINED_COLUMNS):
        # The dedicated widening step already installs the current intervention
        # DDL and translates historical actions; do not copy its history twice.
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
    note = json.dumps({"from": version, "to": SCHEMA_VERSION,
                       "readableFrom": READABLE_FROM, "build": build,
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
                         "'migrate'", "'hold'", "'release_hold'",
                         "'register_project'", "'disable'", "'pause'",
                         "'abort'", "'abort_close'")):
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
    # Built beside the live table and renamed into place, never the live
    # table renamed away: SQLite rewrites every key that points at a renamed
    # table, so `runs.stopRequested` would follow it to a name the DROP
    # then removes (KO-664).
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
    # Create, copy, drop, rename in: see `_widen_interventions_action()`.
    ddl = SCHEMA.split("CREATE TABLE IF NOT EXISTS runEvents (", 1)[1].split(
        ");", 1)[0]
    conn.execute("CREATE TABLE runEvents_new (" + ddl + ")")
    conn.execute(
        "INSERT INTO runEvents_new (id, runId, seq, level, kind, summary,"
        " payload, at, projectId) SELECT id, runId, seq, level, kind, summary,"
        " payload, at, projectId FROM runEvents")
    conn.execute("DROP TABLE runEvents")
    conn.execute("ALTER TABLE runEvents_new RENAME TO runEvents")
