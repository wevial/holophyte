"""store: the v2 durable state store, one WAL-mode SQLite file.

Local-first by resolved decision (2026-08-22): all loop state lives behind
this one module so a hosted backend later is a driver swap, not a loop
rewrite. Stdlib ``sqlite3`` only.

The API so far is ``open()`` and ``init()`` for the schema,
``ensure_project()`` for the repo's projects row, ``claim()``/``release()``
for the per-project lease, ``with_delivery()`` for webhook idempotency,
``mirror_ticket()``/``transition()`` for ticket status,
``pickable()``/``next_pickable()`` for the pickability predicate,
``resume()`` for the resume guidance invariant,
``findings_fingerprint()``/``findings_overlap()`` for stuck-review
detection, ``record_review_round()`` for the rows they read, and
``contract_snapshot()``/``run_contract()``/``contract_drift()`` for the
claim-time freeze of a ticket's contract and the drift check against it.

Conventions, fixed here for every later ticket to follow:

* **camelCase** table and column names, matching the field names in the
  state model verbatim. The table names are camelCase in the contract
  (``reviewRounds``, ``runEvents``, ``linearDeliveries``), so the columns
  follow rather than splitting the file across two casings.
* **List-typed fields are JSON text.** SQLite has no array type, so the
  contract's ``string[]`` and object-array fields are stored as a JSON
  document defaulting to ``'[]'``. ``mirror_ticket()`` encodes the ticket's
  three lists on the way in; decoding belongs to the readers, in the later
  tickets.
* **Optional (``?``) fields are nullable; everything else is NOT NULL.**
* **Union types become CHECK constraints**, so an unknown status, phase or
  verdict is rejected by the database rather than by a caller who
  remembered to look.
* **Rows are keyed by a synthetic ``id INTEGER PRIMARY KEY``**, standing in
  for the contract's Convex-shaped ``Id<table>`` references.

Contract source: docs/v2/state-model.md §1-§3, plus the per-project lease
column from §7. That document is deliberately gitignored, so the sections
are cited here instead of vendored.
"""
from __future__ import annotations

import collections
import contextlib
import hashlib
import json
import socket
import sqlite3
import time

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
    linearIdentifier     TEXT    NOT NULL,  -- e.g. "HOL-142", for humans
    title                TEXT    NOT NULL,
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
                         'failed', 'killed')),
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
               OR outcome IN ('merged', 'killed', 'abandoned', 'failed')),
    outcomeReason     TEXT,
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
                                  'failed', 'killed')),
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
    runId   INTEGER NOT NULL REFERENCES runs (id),
    seq     INTEGER NOT NULL,  -- monotonic per run
    level   TEXT    NOT NULL CHECK (level IN ('narrative', 'detail')),
    kind    TEXT    NOT NULL,  -- 'phase_change' | 'tool_use' | 'supervisor_probe' | ...
    summary TEXT    NOT NULL,  -- human-readable, always present
    payload TEXT,              -- JSON, detail level only
    at      INTEGER NOT NULL,
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

-- linearDeliveries: webhook idempotency (state-model §1). The delivery id is
-- the primary key, so a replayed delivery collides instead of re-running its
-- effect.
CREATE TABLE IF NOT EXISTS linearDeliveries (
    deliveryId  TEXT    PRIMARY KEY,
    processedAt INTEGER NOT NULL
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
    runId     INTEGER NOT NULL REFERENCES runs (id),
    source    TEXT    NOT NULL CHECK (source IN ('supervisor', 'human')),
    "trigger" TEXT    NOT NULL
        CHECK ("trigger" IN ('time_box', 'off_criteria', 'looping',
                             'review_stuck', 'linear_cancelled', 'manual')),
    "action"  TEXT    NOT NULL
        CHECK ("action" IN ('redirect', 'kill', 'extend_time_box', 'resume',
                            'close_out')),
    question  TEXT,  -- for redirect
    guidance  TEXT,  -- human answer, only when the run was blocked_on_operator
    at        INTEGER NOT NULL
)"""


def open(path):  # noqa: A001 - the ticket names this entry point open()
    """Open the store at `path` in WAL mode and return the connection.

    WAL is not advisory here: the supervisor reads a run's state while the
    loop is writing it, and rollback-journal mode would block one on the
    other. A filesystem that cannot honour the pragma (a network mount, say)
    silently leaves the database in its old mode, so the resulting mode is
    read back and a mismatch raises rather than degrading quietly.

    Shadows the builtin `open` inside this module only; callers say
    `store.open(...)`.
    """
    conn = sqlite3.connect(path)
    # Referential integrity is off by default in SQLite and is per-connection,
    # so it has to be asserted on every open, not once at init().
    conn.execute("PRAGMA foreign_keys = ON")
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if mode.lower() != "wal":
        conn.close()
        raise sqlite3.DatabaseError(
            f"{path}: could not enable WAL mode (journal_mode is {mode!r})"
        )
    return conn


# Columns added to a table after its CREATE statement first shipped, as
# (table, column, DDL). SCHEMA is `CREATE TABLE IF NOT EXISTS` throughout,
# which does exactly nothing to a table that already exists — so without this
# list a store initialized by an earlier version of the module keeps its
# original columns forever, and the first reader of a newer one fails with
# `no such column`. Adding a column to SCHEMA is therefore only half the
# change; the other half is an entry here.
#
# Each DDL is transcribed from that column's clause in SCHEMA so a migrated
# database and a fresh one end up with the same column, CHECK included:
# SQLite's ALTER TABLE ADD COLUMN takes a CHECK constraint and enforces it on
# every later write. What it will not take is UNIQUE, or a NOT NULL without a
# constant default — a column needing either wants a table rebuild, not a line
# here. The schema test holds the two databases against each other.
ADDED_COLUMNS = (
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
    # Everything after the executescript rolls back together on failure: a
    # migration that died must not leave an open transaction holding its
    # half-done work, because the next caller's `executescript` would issue
    # an implicit COMMIT and make the half-state durable — the exact hazard
    # `_transaction()`'s docstring warns joined writers about.
    try:
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
        _widen_interventions_action(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _widen_interventions_action(conn):
    """Rebuild `interventions` when its action CHECK predates 'close_out'.

    `CREATE TABLE IF NOT EXISTS` never touches an existing table and SQLite
    cannot ALTER a CHECK, so a store initialized before the value shipped
    would refuse the row forever — which is how the KO-146 incident ended in
    raw SQL and four falsely-labeled 'resume' rows. The stored DDL says which
    world this store is from; the rebuild is the standard rename-copy-drop
    from `_INTERVENTIONS_DDL` itself, run only when needed, so a fresh store
    and a second call both skip it. The column list is unchanged, so existing
    rows are carried verbatim; `runId`'s foreign key stays enforced through
    the copy, which only re-checks rows against `runs` — rows that were valid
    stay valid.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table'"
        " AND name = 'interventions'").fetchone()
    if row is None:
        return  # nothing to widen; the DDL step in init() creates it
    # Scoped to the action CHECK's own value list, not the whole DDL: the
    # literal appearing anywhere else (a future comment, a default) must not
    # skip a rebuild that is still needed.
    (ddl,) = row
    if "'close_out'" in ddl.partition('"action" IN (')[2].partition(")")[0]:
        return
    # The copy runs with foreign keys enforced, so an orphaned row — a
    # `runId` no run has, the kind a raw-SQL session with FKs off leaves —
    # would abort the rebuild at the INSERT. Refusing up front instead names
    # the problem and the fix, and means the copy below cannot half-fail.
    (orphans,) = conn.execute(
        "SELECT COUNT(*) FROM interventions i LEFT JOIN runs r"
        " ON r.id = i.runId WHERE r.id IS NULL").fetchone()
    if orphans:
        raise sqlite3.IntegrityError(
            f"{orphans} interventions row(s) reference runs that do not"
            " exist; repair them before this store can migrate")
    with _transaction(conn):
        conn.execute("ALTER TABLE interventions RENAME TO interventions_old")
        conn.execute(_INTERVENTIONS_DDL)
        conn.execute(
            "INSERT INTO interventions SELECT * FROM interventions_old")
        conn.execute("DROP TABLE interventions_old")


@contextlib.contextmanager
def _transaction(conn):
    """Run the block in one `BEGIN IMMEDIATE`, or join the caller's transaction.

    Every writer in this module is a read (what is there now?) followed by a
    write (change it), which two concurrent callers must not interleave — so
    when this owns the transaction it takes `BEGIN IMMEDIATE`, whose write
    lock is held up front, and callers serialize in SQLite instead of racing.
    It commits on a clean exit and rolls back on any exception, including
    `KeyboardInterrupt` and one raised by the commit itself.

    When a transaction is *already* open the block joins it and this commits
    and rolls back nothing: the owner does both, at its own boundary. That is
    what lets these writers run as the effect of `with_delivery()`, which owns
    a transaction precisely so the delivery id and the effect's writes commit
    or roll back as one. Without it, a nested `BEGIN` would raise
    `OperationalError: cannot start a transaction within a transaction` and no
    Linear delivery could atomically record the ticket write it caused.

    A joined block inherits the owner's locking, so an owner that wants the
    serialization above must have opened its transaction IMMEDIATE too;
    `with_delivery()` and `claim()` do.
    """
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        # Inside the guard, like `with_delivery()`: a deferred constraint the
        # block violated is only checked at COMMIT, and SQLite leaves the
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


class ClaimConflict(Exception):
    """A claim was refused and nothing was written.

    The typed failure of `claim()`: either the project already has an active
    run — v0's single-threading rule (state-model §7), so this caller does not
    get to start another one — or the ticket does not belong to the project
    whose lease was asked for. Both leave every table exactly as it was.
    """


# --- the claim-time contract snapshot -----------------------------------------
# A run is worked to the ticket as it stood when the lease was taken: that
# body is what the implementer was briefed with and what the reviewer judged
# against. Linear keeps letting a human edit it, though, so the claim freezes
# the contract here and the merge gate compares the live ticket against the
# freeze before the branch lands.
#
# The fields are the ones a run is actually held to: the title it was briefed
# with, and the two lists §2's pickability predicate reads. The estimate is
# deliberately not among them — `runs.timeBoxMs` already snapshots it, and a
# re-pointed estimate changes what the run was budgeted, not what it was
# asked to do.
CONTRACT_FIELDS = ("title", "acceptanceCriteria", "verificationCommands")


def contract_snapshot(title, acceptance_criteria, verification_commands):
    """Freeze a ticket's contract as one canonical JSON document.

    Canonical so the same contract is the same bytes on both sides of a
    comparison: keys sorted, no encoder-variable whitespace, and the lists
    left in the order the ticket gives them — a reordered acceptance list is
    an edited ticket, not a formatting accident. Both sides build the document
    through this function rather than assembling their own, which is what
    keeps a drift check from reporting the callers' formatting as drift.
    """
    return json.dumps(
        {
            "title": title,
            "acceptanceCriteria": list(acceptance_criteria),
            "verificationCommands": list(verification_commands),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def run_contract(conn, run_id):
    """The snapshot run `run_id` was claimed under, or None if it has none.

    None is a real answer and not an error: a run claimed before this column
    existed has nothing frozen, and `contract_drift()` reads that as nothing
    to compare rather than as no drift. An unknown `run_id` is a caller bug
    and raises, the way `run_phase()` answers the same mistake.
    """
    row = conn.execute(
        "SELECT ticketSnapshot FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no run {run_id}")
    return row[0]


def contract_drift(before, after):
    """The `CONTRACT_FIELDS` that differ between two snapshots, in field order.

    Empty when the two agree and empty when either is None — an unreadable or
    unrecorded side is a comparison that did not happen, and reporting it as
    drift would block a merge on a Linear outage. The answer is field names
    rather than a bare bool so the caller can say *what* moved: "the ticket
    changed" sends a human to a diff they have to find themselves.
    """
    if before is None or after is None:
        return ()
    was, is_now = json.loads(before), json.loads(after)
    return tuple(f for f in CONTRACT_FIELDS if was.get(f) != is_now.get(f))


def claim(conn, project_id, ticket_id, now=None):
    """Take the project's lease for a new run on `ticket_id`; return its id.

    One `BEGIN IMMEDIATE` transaction, per state-model §7: assert
    `projects.activeRunId IS NULL`, insert the `runs` row in phase `claimed`,
    then point both `projects.activeRunId` and `tickets.activeRunId` at it.

    IMMEDIATE matters. The lease is a read (is it free?) followed by a write
    (take it), and a deferred transaction takes no write lock until the write,
    which leaves room for two claimers to both read "free". IMMEDIATE takes the
    write lock up front, so concurrent claimers serialize in SQLite and the
    second one reads a lease that is already held. Losing is therefore a
    deterministic `ClaimConflict`, not a race.

    Every failure path rolls back, so a lost claim leaves no orphan `runs` row.
    `attempt` is 1 + the ticket's prior runs, making it 1-based. `now` is epoch
    milliseconds for `startedAt`/`lastHeartbeat`, defaulting to the clock.

    The run also takes its own copy of the ticket's `timeBoxMs`, because the
    claim is the moment the estimate applied to this attempt: a later mirror
    of the same Linear issue may carry a re-pointed estimate, and a run row
    that read it back through the ticket would silently restate what it was
    budgeted. A ticket with no estimate snapshots NULL, the same "unknown".

    `ticketSnapshot` is frozen for the same reason and one more: the contract
    the run is worked to is the body as it stood at the claim, so a mirror
    that later re-points the title or either list must not be able to change
    what this run was asked for after the fact. The merge gate reads the
    freeze back through `run_contract()` and compares it with the live ticket.
    """
    if now is None:
        now = int(time.time() * 1000)
    conn.execute("BEGIN IMMEDIATE")
    try:
        # A project row that does not exist matches nothing here and fails a
        # moment later on the runs.projectId foreign key: an unknown project is
        # a malformed claim, not a lease conflict, and reads better as one.
        held = conn.execute(
            "SELECT activeRunId FROM projects"
            " WHERE id = ? AND activeRunId IS NOT NULL",
            (project_id,),
        ).fetchone()
        if held is not None:
            raise ClaimConflict(
                f"project {project_id}: lease already held by run {held[0]}"
            )
        (prior,) = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE ticketId = ?", (ticket_id,)
        ).fetchone()
        # A ticket that does not exist reads as no estimate and no contract
        # here and is refused a moment later by the ownership check below, so
        # this read decides nothing about whether the claim is legal. One
        # SELECT for both snapshots, so the estimate and the contract a run
        # records are the same ticket at the same instant.
        ticket = conn.execute(
            "SELECT timeBoxMs, title, acceptanceCriteria, verificationCommands"
            " FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        estimate = ticket[0] if ticket else None
        snapshot = None if ticket is None else contract_snapshot(
            ticket[1], json.loads(ticket[2]), json.loads(ticket[3]))
        run_id = conn.execute(
            "INSERT INTO runs"
            " (ticketId, projectId, attempt, phase, startedAt, lastHeartbeat,"
            "  timeBoxMs, ticketSnapshot, host)"
            " VALUES (?, ?, ?, 'claimed', ?, ?, ?, ?, ?)",
            (ticket_id, project_id, prior + 1, now, now, estimate, snapshot,
             socket.gethostname()),
        ).lastrowid
        conn.execute(
            "UPDATE projects SET activeRunId = ? WHERE id = ?",
            (run_id, project_id),
        )
        # Scoped by projectId as well as id: claiming another project's ticket
        # would otherwise hand out this project's lease for work it does not
        # own. Zero rows updated means exactly that, and is refused.
        updated = conn.execute(
            "UPDATE tickets SET activeRunId = ? WHERE id = ? AND projectId = ?",
            (run_id, ticket_id, project_id),
        ).rowcount
        if updated != 1:
            raise ClaimConflict(
                f"ticket {ticket_id} is not a ticket of project {project_id}"
            )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return run_id


# §4's phase union, transcribed from the diagram's phase list. It duplicates
# the `runs.phase` CHECK constraint deliberately: the constraint is the
# enforcement, this is what a caller's typo is caught against *before* a
# transaction opens, so a misspelled phase reads as a named ValueError rather
# than as a bare IntegrityError from a table the caller never mentions.
PHASES = (
    "claimed", "working", "verifying", "reviewing", "addressing", "merge_gate",
    "awaiting_merge_approval", "merging", "squashing", "done",
    "blocked_on_operator", "failed", "killed",
)


def set_phase(conn, run_id, phase, note=None, now=None):
    """Move run `run_id` to `phase`; return the phase it was in.

    §6 gives run phase exactly one writer, and this is it for a running loop:
    a stage boundary is three facts — the new phase, a heartbeat proving the
    loop was alive at that boundary, and a narrative `runEvents` row saying so
    — and any of the three landing without the others is a lie about the run.
    A supervisor reading a phase whose heartbeat still sits at the previous
    boundary sees a run that has been stale for a stage; an event stream
    missing the transition its own run row shows is a stream nothing can be
    reconstructed from. So all three go in one `_transaction()`, and a reader
    sees either all of them or none.

    The event is `narrative` level with kind `phase_change`, which is §2's
    dual-level log read literally: transitions are low-volume and drive the
    live view, so they are written individually rather than batched, and
    `payload` stays NULL because §2 gives payloads to `detail` rows only.
    The event's `summary` always opens with `"<previous> -> <phase>"`, so the
    sequence a run walked can be read back off its own stream — a
    `phase_change` row that does not name its phases is a transition nothing
    can reconstruct the run from. An optional `note` is appended after a
    colon for what the phase names alone do not say: which review round, which
    branch.

    `seq` is `MAX(seq) + 1` for the run, read inside the transaction, so the
    `UNIQUE (runId, seq)` index stands behind the per-run monotonicity rather
    than a caller's counter. `now` is epoch milliseconds for `lastHeartbeat`
    and the event's `at`, defaulting to the clock.

    Re-entering the phase a run is already in is allowed and logged: the loop
    verifies once per review round, and collapsing those into one event would
    erase the round boundary the log exists to show. `resume()` is the one
    other phase writer and deliberately does not come through here — it moves
    a parked run without stamping a heartbeat it has no evidence for.
    """
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT phase FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        (previous,) = row
        conn.execute(
            "UPDATE runs SET phase = ?, lastHeartbeat = ? WHERE id = ?",
            (phase, now, run_id),
        )
        _append_event(
            conn, run_id, "narrative", "phase_change",
            f"{previous} -> {phase}" + (f": {note}" if note else ""), now)
    return previous


def run_phase(conn, run_id):
    """Return the phase run `run_id` is in.

    The read half of `set_phase()`, here for the same reason every other
    statement in this module is: a caller that needs the phase a run stopped
    in — to name it in an outcome reason, say — must not open its own SQL
    against `runs` to get it. An unknown `run_id` is a caller bug and raises
    `ValueError`, as it does everywhere else here.
    """
    row = conn.execute("SELECT phase FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"no run {run_id}")
    return row[0]


# §2's two log levels. `narrative` is the run's story and drives the live
# view; `detail` is the volume underneath it, and is the only level §2 gives a
# payload to.
EVENT_LEVELS = ("narrative", "detail")


def _append_event(conn, run_id, level, kind, summary, at):
    """Append one row to run `run_id`'s event stream; return its `seq`.

    No transaction of its own, deliberately: an event describes a thing that
    happened, so it belongs to the transaction of the write it describes —
    `set_phase()` lands the phase, the heartbeat and this row together or not
    at all. `seq` is `MAX(seq) + 1` read inside that transaction, so the
    `UNIQUE (runId, seq)` index stands behind the per-run monotonicity rather
    than a caller's counter.
    """
    (seq,) = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM runEvents WHERE runId = ?",
        (run_id,),
    ).fetchone()
    conn.execute(
        "INSERT INTO runEvents (runId, seq, level, kind, summary, at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, seq, level, kind, summary, at),
    )
    return seq


def record_event(conn, run_id, kind, summary, level="narrative", now=None):
    """Append one event of `kind` to run `run_id`'s stream; return its `seq`.

    `set_phase()` writes the stream's `phase_change` rows and is the only
    writer of a run's phase; this is how the loop writes the rows that are not
    transitions — a best-effort projection that failed, say. `kind` is free
    text because §2's column is a label rather than an enum, `level` is one of
    §2's two, and `payload` stays NULL since that is a `detail`-row field and
    nothing writing through here has one.

    An unknown `run_id` is a caller bug and raises `ValueError`, the way
    `set_phase()` and `run_phase()` answer the same mistake — the foreign key
    would refuse the row anyway, but as an `IntegrityError` naming a
    constraint rather than the run that does not exist. `now` is epoch
    milliseconds for `at`, defaulting to the clock.
    """
    if level not in EVENT_LEVELS:
        raise ValueError(f"unknown event level {level!r}")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        if conn.execute("SELECT 1 FROM runs WHERE id = ?",
                        (run_id,)).fetchone() is None:
            raise ValueError(f"no run {run_id}")
        return _append_event(conn, run_id, level, kind, summary, now)


# The `runs.phase` a run ends in for each `runs.outcome`, so `release()` cannot
# leave a finished run parked in the phase it was working in. `killed` is its
# own phase in §4; the other two failure outcomes share `failed`.
TERMINAL_PHASES = {
    "merged": "done",
    "killed": "killed",
    "abandoned": "failed",
    "failed": "failed",
}

# The phases those outcomes leave behind. A run with `endedAt` stamped and
# sitting in one of them is over, and that pair is what `release()` refuses to
# end a second time. A resumed run is not in the set: `resume()` moves a failed
# run back to a work phase, so the run it hands back is releasable again.
ENDED_PHASES = frozenset(TERMINAL_PHASES.values())

# `runs.outcomeClass`: what a failure is evidence about. Mirrors the CHECK.
OUTCOME_CLASSES = frozenset({"work", "infra"})


def release(conn, run_id, outcome, reason=None, now=None,
            outcome_class="work"):
    """End run `run_id` with `outcome` and give the project's lease back.

    The mirror of `claim()`, and the reason a crashed loop does not brick the
    queue: `projects.activeRunId` is the lease, so a run that ends without
    clearing it blocks every later claim on that project forever. Callers
    therefore release on failure paths too, not only on the happy one.

    One `BEGIN IMMEDIATE`, for the same read-then-write reason `claim()` takes
    it: stamp `endedAt`/`outcome`/`outcomeReason` and the terminal phase
    `TERMINAL_PHASES` gives for the outcome, then clear both `activeRunId`
    fields, moving the ticket's pointer to `lastRunId` so the finished run is
    still reachable from the ticket.

    Through `_transaction()` rather than a `BEGIN` of its own, so a caller
    that has already opened one joins instead of raising. A process failing
    *itself* has nothing to join for -- it is the only writer of its own run
    -- but a process failing somebody else's run has to re-read the state it
    decided on and clear the lease under one lock, or the run heartbeats in
    between and the lease is handed to a second worker while the first is
    still writing. That is the supervisor sweep, and this is where its
    re-check has to be able to sit.

    The run's telemetry is finalized in the same transaction: `endedAt` is
    the other end of the elapsed time `startedAt` opened, and
    `reviewRoundCount` is counted off the run's own `reviewRounds` rows. Both
    are written once, here, so a finished run carries how long it took and how
    many rounds it needed without a reader having to re-derive either.

    The terminal phase moves through `set_phase()`, so ending a run stamps a
    heartbeat and appends the transition to the run's event stream like any
    other phase change. A failure outcome also parks the phase the run stopped
    in as its `resumePhase`, which is the only moment that phase is still
    known: `runs.phase` reads `failed` from here on, and §5 resumes a failed
    run into the phase it left.

    Releasing a run that has already ended — `endedAt` stamped and parked in
    one of `ENDED_PHASES` — does nothing at all. Since this call writes phase
    as well as outcome, an unguarded second release would be destructive
    rather than harmless: it would re-end a `merged`/`done` run as
    `failed`/`failed`, and a repeat of a failed release would read that run's
    own `failed` phase back as the phase it stopped in and so wipe the
    `resumePhase` §5 resumes into. Terminal state and the resume point are
    written once, by the release that ended the run. Both lease clears stay
    scoped to *this* run id for the same reason, so no release can drop a
    lease a newer run has since taken.

    An unknown `run_id` is a caller bug and raises `ValueError`. `now` is
    epoch milliseconds for `endedAt`, defaulting to the clock.

    `outcome_class` is `runs.outcomeClass`: `work` unless the caller knows
    the failure was the factory's own (`infra`), in which case the row is
    kept out of the escalation count. An unknown class raises before any
    write, the same as an unknown outcome.
    """
    if outcome not in TERMINAL_PHASES:
        raise ValueError(f"unknown outcome {outcome!r}")
    if outcome_class not in OUTCOME_CLASSES:
        raise ValueError(f"unknown outcome class {outcome_class!r}")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT ticketId, projectId, endedAt, phase FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        ticket_id, project_id, ended_at, phase = row
        if ended_at is not None and phase in ENDED_PHASES:
            # Already over. Returning leaves the block having written nothing
            # rather than re-stamping an ending over the real one; an owned
            # transaction commits empty, and a joined one is the caller's to
            # end either way.
            return
        # Through `set_phase()` like every other phase move, so the run's last
        # transition is in its event stream too: a merged run whose log stops
        # at `merging` reads as a run that never finished.
        stopped_in = set_phase(conn, run_id, TERMINAL_PHASES[outcome],
                               note=f"run ended, outcome {outcome}", now=now)
        # §5's "it re-enters the phase it left", recorded here because
        # `release()` is the last caller that still knows what that phase was:
        # after this write the run says `failed` and nothing else remembers
        # where the work had got to. Only the four phases §5 calls mechanically
        # resumable are worth recording — a run that failed while `claimed` or
        # mid-merge has no work phase to go back to, and `resume()` reads the
        # NULL as §4's edge back to `working`.
        resume_phase = (stopped_in
                        if TERMINAL_PHASES[outcome] == "failed"
                        and stopped_in in RESUMABLE_WORK_PHASES else None)
        # `reviewRoundCount` is stamped here, from the rows themselves, for
        # the same reason the phase is: this is the close-out, so this is the
        # moment the count is final. Counting the run's own `reviewRounds`
        # rather than trusting a caller's tally keeps the column from
        # disagreeing with the rounds it summarizes.
        conn.execute(
            "UPDATE runs SET endedAt = ?, outcome = ?, outcomeReason = ?,"
            " outcomeClass = ?, resumePhase = ?,"
            " reviewRoundCount = (SELECT COUNT(*) FROM reviewRounds"
            "                     WHERE runId = ?)"
            " WHERE id = ?",
            (now, outcome, reason, outcome_class, resume_phase, run_id,
             run_id),
        )
        conn.execute(
            "UPDATE projects SET activeRunId = NULL"
            " WHERE id = ? AND activeRunId = ?",
            (project_id, run_id),
        )
        conn.execute(
            "UPDATE tickets SET activeRunId = NULL, lastRunId = ?"
            " WHERE id = ? AND activeRunId = ?",
            (run_id, ticket_id, run_id),
        )


def ensure_project(conn, linear_team_id, repo_path, default_branch="main",
                   autonomy_profile="personal"):
    """Return the id of the projects row for `linear_team_id`, creating it once.

    Every other write in this module needs a `projectId`, and §2 gives no
    other way to make the first one: a loop starting against a fresh store has
    to be able to bootstrap its own project row. `linearTeamId` is the row's
    natural key here because it is the table's UNIQUE column, so a second call
    for the same team returns the existing row rather than racing a duplicate.

    Existing rows are returned untouched. Re-pointing a project at another repo
    path or another autonomy profile is a policy change, not a side effect of
    starting a loop, so it is not done here.
    """
    with _transaction(conn):
        row = conn.execute(
            "SELECT id FROM projects WHERE linearTeamId = ?", (linear_team_id,)
        ).fetchone()
        if row is not None:
            return row[0]
        return conn.execute(
            "INSERT INTO projects"
            " (linearTeamId, repoPath, defaultBranch, autonomyProfile)"
            " VALUES (?, ?, ?, ?)",
            (linear_team_id, str(repo_path), default_branch, autonomy_profile),
        ).lastrowid


# What `with_delivery()` hands back. A namedtuple rather than a bare value
# because "the effect returned None" and "the effect never ran" are different
# answers a webhook handler acts on differently, and no sentinel return value
# can tell them apart when the effect is free to return anything.
Delivery = collections.namedtuple("Delivery", ("replayed", "result"))


def with_delivery(conn, delivery_id, effect, now=None):
    """Run `effect(conn)` exactly once per `delivery_id`; return a `Delivery`.

    State-model §1: every inbound Linear delivery id is recorded in
    `linearDeliveries` *in the same transaction as its effect*. That sharing is
    the whole primitive. A check-then-act split across two transactions still
    races two concurrent copies of the same redelivery, and recording the id in
    its own transaction would burn it even when the effect went on to fail.

    So: one `BEGIN IMMEDIATE`, insert the id, run the effect, commit. The three
    outcomes are

    * fresh id — `Delivery(replayed=False, result=<what the effect returned>)`,
      the effect's writes and the delivery row committed together;
    * duplicate id — the insert raises `IntegrityError`, the effect never runs,
      the transaction rolls back, and the result is `Delivery(True, None)`.
      Nothing is written, so the original `processedAt` is not restamped;
    * the effect raises, or the commit does — everything rolls back,
      *including the delivery id*, and the exception propagates. Linear's next
      redelivery is processed rather than swallowed, which is the point of the
      shared transaction, and the connection is left usable for that retry.

    Only the insert is guarded, never the effect: an `IntegrityError` from the
    effect's own writes is a real failure, and reporting it as a replay would
    silently drop a delivery that was never processed.

    `effect` must confine itself to `conn` and must not commit, roll back or
    open its own transaction; doing so breaks the atomicity this exists for.
    The store's own writers satisfy that by construction — `mirror_ticket()`
    and `transition()` go through `_transaction()`, which joins an open
    transaction instead of starting one — so `lambda c: mirror_ticket(c, ...)`
    is the intended shape of an effect, not a special case.
    `now` is epoch milliseconds for `processedAt`, defaulting to the clock.
    """
    # SQLite allows NULL in a TEXT PRIMARY KEY, and allows it repeatedly, so an
    # absent id would silently process every replay instead of colliding on the
    # second one. Refuse it here rather than let the dedup quietly not happen.
    if not isinstance(delivery_id, str) or not delivery_id:
        raise ValueError(f"delivery id must be a non-empty string, got {delivery_id!r}")
    if now is None:
        now = int(time.time() * 1000)
    conn.execute("BEGIN IMMEDIATE")
    try:
        try:
            conn.execute(
                "INSERT INTO linearDeliveries (deliveryId, processedAt)"
                " VALUES (?, ?)",
                (delivery_id, now),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            return Delivery(replayed=True, result=None)
        result = effect(conn)
        # Inside the guard: a deferred constraint the effect violated is only
        # checked here, and SQLite leaves the transaction *open* when COMMIT
        # fails that way. Without the rollback the id would be reserved but
        # uncommitted, and the connection's next BEGIN IMMEDIATE would raise
        # "cannot start a transaction within a transaction" — so the retry that
        # should process the delivery could not even start.
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return Delivery(replayed=False, result=result)


# The §3 status diagram, transcribed edge for edge, plus the one edge below
# that the diagram does not draw:
#
#     needs_spec → ready → in_flight → merged
#                     ↕         ↓        ↘
#              blocked_on_deps  │   abandoned
#                     ↕         │
#             blocked_on_operator ←┘
#
# Read as data so the table below can be diffed against the drawing: each key
# is a status, each value the statuses it may move to. `merged` and
# `abandoned` are terminal, so they map to empty sets rather than being left
# out — a missing key and "nowhere to go" would otherwise be the same lookup.
#
# The one reading the drawing does not spell out is the `↘`, which hangs off
# the `in_flight → merged` edge: a run either merges or is given up on, and
# `merged` is done (§3's table: "done"), so it is `in_flight → abandoned`, not
# `merged → abandoned`.
#
# `in_flight → blocked_on_operator` is the added edge, and it is Holophyte's
# rather than the doc's: §3 gives an in-flight ticket only the two endings
# above, so a ticket the loop keeps failing on had nowhere to go that says
# "stop claiming this, a human owns it now". `abandoned` will not do — it is
# terminal, and a repeatedly failing ticket is not a decision to give up, it is
# a decision nobody has made yet. `blocked_on_operator` already means exactly
# that, and already has the way back out (→ blocked_on_deps → ready) an
# unblocking human needs, so failure-pattern escalation walks into it from
# in_flight instead of inventing a seventh status.
#
# A status is not in its own set: `ready → ready` is refused like any other
# non-edge, so a no-op status write cannot pass for a real transition.
TICKET_TRANSITIONS = {
    "needs_spec": frozenset({"ready"}),
    "ready": frozenset({"in_flight", "blocked_on_deps"}),
    "in_flight": frozenset({"merged", "abandoned", "blocked_on_operator"}),
    "blocked_on_deps": frozenset({"ready", "blocked_on_operator"}),
    "blocked_on_operator": frozenset({"blocked_on_deps"}),
    "merged": frozenset(),
    "abandoned": frozenset(),
}

# Derived, not re-typed, so the enum cannot drift from the transition table.
# It must still match the `tickets.status` CHECK in SCHEMA above; the tests
# assert that against the database rather than trusting the agreement.
TICKET_STATUSES = tuple(TICKET_TRANSITIONS)


def render_state_graph(transitions):
    """Render a `{state: {next, ...}}` table as Mermaid `stateDiagram-v2` text.

    Pure and deterministic: nodes are the table's keys in sorted order, edges
    are every `(from, to)` pair in sorted order, one per line, and nothing
    else is drawn. README embeds the output for `TICKET_TRANSITIONS` and
    `RUN_PHASE_TRANSITIONS` between marker comments, and a test re-renders
    both from the live tables and asserts byte equality, so the drawing
    cannot drift from the code the way the prose diagram did. A state that
    appears only as a target is still declared as a node by its own key, so
    a table missing a key renders that state as an edge end only.

    `python3 store.py --state-graph` prints both blocks with their markers,
    ready to paste over README's sections.
    """
    lines = ["stateDiagram-v2"]
    lines.extend(f"    {state}" for state in sorted(transitions))
    lines.extend(f"    {src} --> {dst}"
                 for src in sorted(transitions)
                 for dst in sorted(transitions[src]))
    return "\n".join(lines) + "\n"


# The README sections `--state-graph` prints, in README order: marker name to
# the table it draws. The markers are HTML comments so GitHub renders only the
# fenced Mermaid between them.
STATE_GRAPHS = (
    ("state-graph: tickets", "TICKET_TRANSITIONS"),
    ("state-graph: runs", "RUN_PHASE_TRANSITIONS"),
)


def render_state_graph_section(name, transitions):
    """The exact README text for one marked section, markers included."""
    return (f"<!-- {name} -->\n```mermaid\n{render_state_graph(transitions)}"
            f"```\n<!-- end {name} -->\n")


class IllegalTransition(Exception):
    """A status change the §3 diagram does not draw; nothing was written.

    Also raised for an unknown target status and for a ticket id that does not
    exist — neither names an edge of the diagram, and both are the same
    mistake from the caller's side: a status change that will not happen.
    """


def _json_list(field, values):
    """Encode a contract `string[]` field as the JSON text the schema stores.

    A bare `str` is rejected rather than encoded: it is the easy mistake here,
    it is iterable, and a JSON string is truthy where the empty list is not —
    so passing one would route an under-specced ticket to `ready`.
    """
    if isinstance(values, str):
        raise ValueError(f"{field} must be a list of strings, got {values!r}")
    items = list(values)
    bad = [v for v in items if not isinstance(v, str)]
    if bad:
        raise ValueError(f"{field} must contain only strings, got {bad[0]!r}")
    return json.dumps(items)


def transition(conn, ticket_id, to_status):
    """Move `ticket_id` to `to_status`; return the status it came from.

    Legality is `TICKET_TRANSITIONS`, i.e. state-model §3, and nothing else.
    An illegal move raises `IllegalTransition` and leaves the row untouched.

    One `_transaction()` for the same reason as `claim()`: this is a read
    (where is the ticket now?) followed by a write (move it), and two
    concurrent callers must not both read the same `from` status and both act
    on it. `BEGIN IMMEDIATE` takes the write lock up front, so they serialize
    and the second one validates against the status the first one wrote. When
    the caller already owns a transaction the move joins it and commits with
    it, which is what makes this usable as a `with_delivery()` effect: a
    Linear status webhook records its delivery id and the status change
    together or not at all.

    The previous status is returned because the caller usually has to log or
    mirror the change, and re-reading it afterwards cannot recover it.
    """
    with _transaction(conn):
        row = conn.execute(
            "SELECT status FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise IllegalTransition(f"ticket {ticket_id} does not exist")
        (from_status,) = row
        if to_status not in TICKET_TRANSITIONS.get(from_status, frozenset()):
            raise IllegalTransition(
                f"ticket {ticket_id}: {from_status} -> {to_status} is not a"
                " transition the state-model §3 diagram draws"
            )
        conn.execute(
            "UPDATE tickets SET status = ? WHERE id = ?", (to_status, ticket_id)
        )
    return from_status


def _status_path(from_status, to_status):
    """Shortest §3 path as the statuses to walk through, or None."""
    frontier, seen = [(from_status, ())], {from_status}
    while frontier:
        status, path = frontier.pop(0)
        for nxt in sorted(TICKET_TRANSITIONS[status]):
            if nxt == to_status:
                return (*path, nxt)
            if nxt not in seen:
                seen.add(nxt)
                frontier.append((nxt, (*path, nxt)))
    return None


def walk_ticket(conn, ticket_id, to_status):
    """Move `ticket_id` to `to_status` along §3 edges; return the path taken.

    The operator's `transition()`. The diagram stays the only authority: the
    walk is a shortest path over `TICKET_TRANSITIONS` taken edge by edge
    through `transition()` inside one `_transaction()`, so no edge exists
    here that the diagram does not draw — and the KO-146-style repair
    ("ready but the work is merged") is one call instead of a hand-found
    path taken in raw SQL. Already there returns `()`; no path raises
    `IllegalTransition` before any edge is taken, so nothing is written.
    A path may transit blocked statuses where the diagram routes through
    them (`in_flight -> ready` passes `blocked_on_operator`): the walk is
    §3 legality, and what a transit status *means* stays the caller's
    judgment.
    """
    if to_status not in TICKET_TRANSITIONS:
        raise IllegalTransition(f"unknown status {to_status!r}")
    with _transaction(conn):
        row = conn.execute("SELECT status FROM tickets WHERE id = ?",
                           (ticket_id,)).fetchone()
        if row is None:
            raise IllegalTransition(f"ticket {ticket_id} does not exist")
        (from_status,) = row
        if from_status == to_status:
            return ()
        path = _status_path(from_status, to_status)
        if path is None:
            raise IllegalTransition(
                f"ticket {ticket_id}: no §3 path from {from_status}"
                f" to {to_status}")
        for status in path:
            transition(conn, ticket_id, status)
    return path


def mirror_ticket(
    conn,
    project_id,
    linear_issue_id,
    linear_identifier,
    title,
    acceptance_criteria=(),
    verification_commands=(),
    time_box_ms=None,
    affinity="any",
    depends_on=None,
    now=None,
):
    """Upsert the Holophyte mirror of a Linear issue; return its ticket id.

    The routing rule, state-model §2: a ticket lacking acceptance criteria or
    a verification command is **not pickable**, so a new one lands in
    `needs_spec`; one carrying both lands in `ready`. That is a data
    invariant, not a prompt instruction — the loop cannot pick up an
    under-specced ticket because the store never gives it a pickable one.
    The criteria/command *defaults are empty* for the same reason: a caller
    that forgets to pass them gets the unpickable answer, not the pickable one.

    On re-mirror the Linear-owned fields are refreshed and the status is left
    alone, with one exception: between `needs_spec` and `ready` the status
    follows the body, in both directions. Those two are the statuses §2
    derives from the body alone — `ready` *means* the row carries both lists
    — so a ticket promoted once the body finally carries them is demoted
    again if an edit empties them, or if the caller withholds them because
    the body failed the template (KO-188): a row that says `ready` about a
    contract it no longer holds is the invariant broken. Neither ticket is
    anybody's work yet. §1 gives Holophyte the in-flight substate, so a
    mirror push may not drag a running ticket backwards, and every other
    status change is somebody's decision and goes through `transition()`.
    In particular `blocked_on_deps → ready` is *not* taken here: it is the
    dependency resolver's call, not a side effect of a body edit.

    `depends_on=None` (the default) means the caller has no opinion about the
    dependency list, not that the list is empty: a new ticket gets `[]`, and a
    re-mirror keeps whatever the row already holds. The loop's claim-time
    re-mirror carries the live body and nothing about dependencies — the
    provider does not parse them — so a default that wrote `[]` would clear a
    blocked ticket's list in the very row `pickable()` reads next, and the
    gate would let it through. A caller that does know the list passes it,
    `[]` included, and that replaces the stored one.

    Lookups are scoped to `project_id`, so re-mirroring another project's
    issue does not overwrite it — it fails on the `linearIssueId` uniqueness
    constraint instead. `now` is epoch milliseconds for `mirroredAt`,
    defaulting to the clock.

    The upsert runs in one `_transaction()`, so it joins a transaction the
    caller already owns rather than opening its own. Mirroring is the effect
    of an inbound Linear issue webhook, and §1 wants that effect and its
    delivery id committed together, so `with_delivery(conn, id, lambda c:
    mirror_ticket(c, ...))` has to work — the argument validation above still
    raises before any transaction is touched.
    """
    criteria = _json_list("acceptance_criteria", acceptance_criteria)
    commands = _json_list("verification_commands", verification_commands)
    depends = None if depends_on is None else _json_list("depends_on", depends_on)
    specced = bool(json.loads(criteria)) and bool(json.loads(commands))
    derived = "ready" if specced else "needs_spec"
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT id, status FROM tickets"
            " WHERE linearIssueId = ? AND projectId = ?",
            (linear_issue_id, project_id),
        ).fetchone()
        if row is None:
            ticket_id = conn.execute(
                "INSERT INTO tickets"
                " (projectId, linearIssueId, linearIdentifier, title, status,"
                "  acceptanceCriteria, verificationCommands, timeBoxMs,"
                "  affinity, dependsOn, mirroredAt)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id, linear_issue_id, linear_identifier, title,
                    derived, criteria, commands, time_box_ms, affinity,
                    "[]" if depends is None else depends, now,
                ),
            ).lastrowid
        else:
            ticket_id, status = row
            if status in ("needs_spec", "ready"):
                status = derived
            conn.execute(
                "UPDATE tickets SET linearIdentifier = ?, title = ?,"
                " status = ?, acceptanceCriteria = ?, verificationCommands = ?,"
                " timeBoxMs = ?, affinity = ?,"
                " dependsOn = COALESCE(?, dependsOn), mirroredAt = ?"
                " WHERE id = ?",
                (
                    linear_identifier, title, status, criteria, commands,
                    time_box_ms, affinity, depends, now, ticket_id,
                ),
            )
    return ticket_id


# The answer `pickable()` gives back. A namedtuple so a caller that wants the
# diagnostics can read `.reason`, with `__bool__` overridden so the common
# `if store.pickable(conn, t):` reads the verdict and not the mere existence
# of the tuple — a plain namedtuple is always truthy, which would turn every
# unpickable ticket into a pickable one at the one call site that matters.
class Pickability(collections.namedtuple("Pickability", ("pickable", "reason"))):
    """`bool(...)` is the predicate; `.reason` names the clause that failed.

    `reason` is None exactly when the ticket is pickable, and otherwise a
    human-readable string naming the *first* failing clause in the order §2
    writes them — a diagnostic, not a parseable code.
    """

    __slots__ = ()

    def __bool__(self):
        return self.pickable


def pickable(conn, ticket_id):
    """Is `ticket_id` claimable right now? Returns a `Pickability`.

    State-model §2's predicate, one function and one truth, transcribed
    clause for clause::

        status == 'ready'
          && activeRunId == null
          && acceptanceCriteria.length > 0
          && verificationCommands.length > 0
          && all(dependsOn).status == 'merged'

    The two list clauses are re-checked here rather than trusted to
    `mirror_ticket()`'s `needs_spec` routing. The rule is that an
    under-specced ticket is not pickable *ever*, and a status column that
    somebody wrote directly is not evidence about the lists — so the
    predicate reads the lists.

    `dependsOn` holds linearIssueIds, resolved within the ticket's own
    project. Empty passes vacuously. A dep naming an issue this store has not
    mirrored is *not* pickable: an unmirrored dep cannot be shown to be
    merged, and the fail-closed answer is the safe one for a gate. Cycle
    detection is out of scope (the doc defers it); a cycle here simply means
    neither ticket is ever pickable, which is the honest answer.

    A ticket id that does not exist is not pickable either, for the same
    reason: the gate answers "no" rather than raising, since every caller is
    asking whether to start work.

    Read-only, and deliberately not transactional. This is the gate's
    question, not its answer: `claim()` takes `BEGIN IMMEDIATE` and re-asserts
    the lease it needs, so a ticket that goes unpickable between the two calls
    loses at the claim, not on a lock held here.
    """
    row = conn.execute(
        "SELECT projectId, status, activeRunId, acceptanceCriteria,"
        " verificationCommands, dependsOn FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()
    if row is None:
        return Pickability(False, f"ticket {ticket_id} does not exist")
    return _pickability(conn, row)


def _pickability(conn, row):
    """Evaluate §2's clauses over one already-fetched `tickets` row.

    Split out only so `next_pickable()` can walk candidate rows through the
    exact same predicate instead of re-deriving any part of it in SQL.
    """
    project_id, status, active_run_id, criteria, commands, depends_on = row
    if status != "ready":
        return Pickability(False, f"status is {status}, not ready")
    if active_run_id is not None:
        return Pickability(False, f"run {active_run_id} is already active on it")
    if not json.loads(criteria):
        return Pickability(False, "it has no acceptance criteria")
    if not json.loads(commands):
        return Pickability(False, "it has no verification commands")
    for dep in json.loads(depends_on):
        dep_row = conn.execute(
            "SELECT status FROM tickets"
            " WHERE linearIssueId = ? AND projectId = ?",
            (dep, project_id),
        ).fetchone()
        if dep_row is None:
            return Pickability(False, f"it depends on {dep}, which is not mirrored")
        if dep_row[0] != "merged":
            return Pickability(
                False, f"it depends on {dep}, which is {dep_row[0]}, not merged"
            )
    return Pickability(True, None)


def next_pickable(conn, project_id):
    """Return the id of the project's next pickable ticket, or None.

    The v0 flat queue: every ticket of the project in ascending `id` order —
    mirror order, so the ticket seen first is offered first — and the first
    one `pickable()` accepts wins. Ascending `id` rather than `mirroredAt`
    because a re-mirror restamps `mirroredAt`, and editing a ticket's body
    should not move it in the queue.

    Deliberately no SQL prefilter on the cheap clauses: every candidate goes
    through the same `pickable()` predicate, so there is one implementation of
    §2 to keep correct rather than two that can drift apart.

    Says nothing about whether the project may start a run at all — the §7
    single-threading lease is `claim()`'s assertion, and re-checking it here
    would only make it look like this answer could be trusted without it.
    """
    rows = conn.execute(
        "SELECT id, projectId, status, activeRunId, acceptanceCriteria,"
        " verificationCommands, dependsOn FROM tickets"
        " WHERE projectId = ? ORDER BY id",
        (project_id,),
    ).fetchall()
    for row in rows:
        if _pickability(conn, row[1:]):
            return row[0]
    return None


# §5's resumable set, transcribed: "mechanically resumable — `failed`, or any
# of working/verifying/reviewing/addressing where lastHeartbeat is older than
# staleThresholdMs", plus `blocked_on_operator`, the one phase that takes an
# answer. Staleness is deliberately not re-derived here: §5 says resume is
# always safe to attempt, and whether a live run *should* be resumed is the
# supervisor's judgement, not a fact this mutation can improve on.
#
# Everything else is refused. `claimed` has nothing to resume into, and
# `merge_gate`, `awaiting_merge_approval`, `merging`, `squashing`, `done` and
# `killed` are either mid-merge or over: none of them is a phase the §4
# diagram draws a resume edge out of.
#
# The set is split because the working half is also what `release()` records
# as a failed run's `resumePhase`: `failed` and `blocked_on_operator` are
# phases a run is parked *in*, not phases work was interrupted in, so neither
# is a phase to send a resumed run back to.
RESUMABLE_WORK_PHASES = frozenset(
    {"working", "verifying", "reviewing", "addressing"}
)
RESUMABLE_PHASES = RESUMABLE_WORK_PHASES | {"failed", "blocked_on_operator"}

# §4's run graph as an edge table, keyed like `TICKET_TRANSITIONS` so both
# state machines render through `render_state_graph()` the same way. The
# edges are the ones the loop writes — `factory.py`'s `set_phase()` calls,
# `release()`'s move into the terminal phase for each outcome, and `resume()`'s
# way back out of a parked run — not a `set_phase()` gate: that function moves
# a run between any two phases on purpose, so the table is the loop's map and
# the wiring tests hold the walked streams against it. Every phase in `PHASES`
# is a key so a declared phase always renders as a node; `awaiting_merge_approval`
# and `squashing` are declared but have no edge because this loop never enters
# them (the merge is --no-ff, and the autonomy gate refuses rather than parks).
RUN_PHASE_TRANSITIONS = {
    "claimed": frozenset({"working", "failed", "killed"}),
    "working": frozenset({"verifying", "failed", "killed"}),
    "verifying": frozenset({"reviewing", "failed", "killed"}),
    "reviewing": frozenset({"addressing", "merge_gate", "failed", "killed"}),
    "addressing": frozenset({"verifying", "failed", "killed"}),
    "merge_gate": frozenset({"merging", "failed", "killed"}),
    "awaiting_merge_approval": frozenset(),
    "merging": frozenset({"done", "failed", "killed"}),
    "squashing": frozenset(),
    "done": frozenset(),
    # `resume()`: a failed run re-enters its `resumePhase`, or `working` when
    # none was recorded; a parked run always re-enters `working`.
    "failed": RESUMABLE_WORK_PHASES,
    "blocked_on_operator": frozenset({"working"}),
    "killed": frozenset(),
}
assert set(RUN_PHASE_TRANSITIONS) == set(PHASES)


class ResumeRefused(Exception):
    """A resume the state model does not allow; nothing was written.

    Raised for a run that does not exist and for one in a phase §5 gives no
    resume for — both are the same answer to the caller: this run is not
    going to start moving again because you asked.
    """


class GuidanceNotAccepted(ResumeRefused):
    """Human text was offered to a run that never asked for it.

    §5's enforced invariant, and the reason this is a subclass rather than a
    return value: guidance landing on a `working` run is the mid-run steering
    injection the whole phase model exists to prevent, so it is a validation
    error and the run is left exactly as it was.
    """


def resume(conn, run_id, guidance=None, source="human", now=None):
    """Resume `run_id`, optionally with `guidance`; return the phase re-entered.

    State-model §5. Two rules, and the first one is the point of the ticket:

    * **Guidance requires `blocked_on_operator`.** A non-None `guidance` on a
      run in any other phase raises `GuidanceNotAccepted` before anything is
      written. Mid-run injection is what makes supervisors unpredictable, so
      the supervisor's `redirect` has to park the run with a question and wait
      for the answer to come back through this one door. The converse is not a
      rule: a bare resume of a blocked run is allowed, an operator saying
      "never mind, carry on".
    * **A bare resume re-enters the phase the run left.** For `failed` that is
      `runs.resumePhase`, recorded by whoever failed it, falling back to
      `working` — the only edge §4 draws out of `failed` — when nothing was
      recorded. A run parked in `blocked_on_operator` always re-enters
      `working` (§4 again: `blocked_on_operator --> working : guidance
      provided`); an answered question resumes as work whatever the run was
      doing when it stopped to ask. And a stale `working`/`verifying`/
      `reviewing`/`addressing` run re-enters the phase it is already in, which
      is that same rule with nothing to move.

    `resumePhase` is cleared on the way out, so a later failure that records
    nothing cannot resume into a phase left over from an earlier one.

    Every accepted resume writes an `interventions` row — §2 keeps those out
    of `runEvents` precisely because they are queryable decisions, and a
    resume is one whether or not a human typed anything. `source` says who
    resumed (`human` or `supervisor`); the trigger is `manual` because §6's
    triggers name why a run was *stopped* and none of them names a resume.

    Phase is the only field this moves. A resumed run's `lastHeartbeat` stays
    where the worker left it: heartbeats are written by whoever is doing the
    work, and stamping one here would claim liveness this call has no evidence
    for. `now` is epoch milliseconds for the intervention's `at`, defaulting
    to the clock.

    Runs in one `_transaction()`, like the other writers, so a resume arriving
    as the effect of a Linear webhook commits with its delivery id.
    """
    # An empty string is not an answer, and it is falsy, so a caller that let
    # one through would have its "no guidance" and its "guidance" paths
    # silently agree here while §5 says they are different calls.
    if guidance is not None and (
        not isinstance(guidance, str) or not guidance.strip()
    ):
        raise ValueError(f"guidance must be non-empty text or None, got {guidance!r}")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT phase, resumePhase FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise ResumeRefused(f"run {run_id} does not exist")
        phase, resume_phase = row
        # The guidance gate is asked first: on a `done` run offered guidance
        # both rules are broken, and the one worth naming is the injection.
        if guidance is not None and phase != "blocked_on_operator":
            raise GuidanceNotAccepted(
                f"run {run_id} is in phase {phase}, not blocked_on_operator:"
                " guidance is only accepted by a run that asked for it"
            )
        if phase not in RESUMABLE_PHASES:
            raise ResumeRefused(
                f"run {run_id}: phase {phase} is not one state-model §5 resumes"
            )
        if phase == "failed" and resume_phase is not None:
            target = resume_phase
        elif phase in ("failed", "blocked_on_operator"):
            target = "working"
        else:
            target = phase
        conn.execute(
            "UPDATE runs SET phase = ?, resumePhase = NULL WHERE id = ?",
            (target, run_id),
        )
        conn.execute(
            'INSERT INTO interventions'
            ' (runId, source, "trigger", "action", guidance, at)'
            " VALUES (?, ?, 'manual', 'resume', ?, ?)",
            (run_id, source, guidance, now),
        )
    return target


# §2's intervention unions, transcribed from `_INTERVENTIONS_DDL` so a caller
# can validate before the INSERT answers with a constraint name instead of
# the value that was wrong. The schema test holds these against the database.
INTERVENTION_SOURCES = ("supervisor", "human")
INTERVENTION_TRIGGERS = ("time_box", "off_criteria", "looping",
                         "review_stuck", "linear_cancelled", "manual")
INTERVENTION_ACTIONS = ("redirect", "kill", "extend_time_box", "resume",
                        "close_out")


def record_intervention(conn, run_id, action, note, source="human",
                        trigger="manual", question=None, guidance=None,
                        now=None):
    """Record one operator/supervisor decision on `run_id`; return its id.

    §2 keeps interventions out of runEvents because they are queryable
    decisions — but the only writer was `resume()`, so every other human
    action was either unrecorded or falsely recorded as a resume (the KO-146
    incident's four mislabeled rows). This is the general writer: the row and
    a narrative runEvent carrying `note` land in one `_transaction()`, so the
    record-before-acting discipline is one call — and a caller that wants the
    record atomic with the change it describes opens `transaction()` around
    both, which this joins.

    `question` is required for a redirect (§2 pairs the two; a redirect row
    with nothing asked would be semantically invalid with no way to repair
    it) and `guidance` carries a human's answer where one exists. `note` is
    the narrative and deliberately lands in the event stream, not the row:
    the columns keep their §2 meanings instead of doubling as a notes field.
    """
    if action not in INTERVENTION_ACTIONS:
        raise ValueError(f"unknown intervention action {action!r}")
    if source not in INTERVENTION_SOURCES:
        raise ValueError(f"unknown intervention source {source!r}")
    if trigger not in INTERVENTION_TRIGGERS:
        raise ValueError(f"unknown intervention trigger {trigger!r}")
    if not isinstance(note, str) or not note.strip():
        raise ValueError(f"note must be non-empty text, got {note!r}")
    if action == "redirect" and (
            not isinstance(question, str) or not question.strip()):
        raise ValueError("a redirect records the question it asked;"
                         f" got {question!r}")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        if conn.execute("SELECT 1 FROM runs WHERE id = ?",
                        (run_id,)).fetchone() is None:
            raise ValueError(f"no run {run_id}")
        cursor = conn.execute(
            'INSERT INTO interventions'
            ' (runId, source, "trigger", "action", question, guidance, at)'
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, source, trigger, action, question, guidance, now))
        _append_event(conn, run_id, "narrative", "intervention",
                      f"{source} {action}: {note}", now)
    return cursor.lastrowid


# §2's severity union, transcribed. Checked here because `reviewRounds.findings`
# is a JSON document rather than rows, so no CHECK constraint stands behind it:
# this helper is the only place a severity typo can be caught before it changes
# a fingerprint and, through it, whether the review looks converged.
SEVERITIES = ("p0", "p1", "p2", "nit")

# The canonical record's separators. §2 writes the key as `path:line:severity`,
# but a colon is a legal character in a path, so joining on one would let
# `{"path": "a:1", "line": 2}` and `{"path": "a", "line": ...}` collide into
# the same record. The ASCII separators are not characters a reviewer cites a
# path by, which is what makes them unambiguous — but "not expected" is not
# "cannot happen", and `findings` is a decoded JSON document, so a path may
# carry any character at all. `_finding_keys()` therefore *rejects* them rather
# than trusting their absence: a path holding a separator could otherwise forge
# a record boundary and hash a round to another round's fingerprint. Rejecting
# keeps the encoding escape-free, so digests already stored stay valid.
_FIELD_SEP = "\x1f"   # ASCII unit separator
_RECORD_SEP = "\x1e"  # ASCII record separator

# A finding with no line is keyed at -1: absent has to be *some* value, and a
# sentinel outside the range of real line numbers keeps "the whole file" and
# "line 1" distinct instead of folding one into the other. It is only outside
# that range because `_finding_keys()` rejects non-positive lines, so no
# reviewer can hand us a -1 that collides with "no line" and makes two
# different findings fingerprint alike.
_NO_LINE = -1

# The fingerprint of a round that found nothing: sha256 of the empty canonical
# form, which is what the general path already computes for an empty set. Named
# because callers compare against it ("did this round find anything?") and
# pinned as a literal because it is written into a NOT NULL column and compared
# across rounds recorded by different releases — this value cannot drift.
EMPTY_FINGERPRINT = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)


def _finding_keys(findings):
    """Normalize `findings` to the set of `(path, line, severity)` keys.

    The key is §2's, and what it leaves out is the point: `message` and
    `criterion` are prose a reviewer rewrites every round, so including them
    would make an unchanged complaint look like a new one and hide exactly the
    non-convergence the fingerprint exists to catch.

    A **set**, so two findings sharing a key collapse into one. Under this key
    they are the same complaint about the same place, and duplicates would
    otherwise make the fingerprint depend on how many times the reviewer said
    it. It also keeps `findings_fingerprint()` and `findings_overlap()` reading
    the same canonical input, which is what makes them comparable at all.

    Raises `ValueError` for a finding missing `path` or `severity`, carrying a
    severity outside §2's union, citing a line below 1, or naming a path that
    contains one of the canonical form's ASCII separators — a fingerprint over
    malformed findings would be a number that compares fine and means nothing.
    Lines are 1-based, so a 0 or a negative is junk either way; rejecting it
    also keeps `_NO_LINE` unreachable as an explicit value. A separator inside
    a path is rejected for the sharper reason that the canonical form does not
    escape them: `[("a", 1, "p0"), ("b", 2, "p1")]` and the single finding
    `("a\x1f1\x1fp0\x1eb", 2, "p1")` would otherwise serialize to the same
    bytes and hash alike, which is §6 reading two rounds that share nothing as
    the same round twice.
    """
    keys = set()
    for finding in findings:
        try:
            path = finding["path"]
            severity = finding["severity"]
        except (TypeError, KeyError) as exc:
            raise ValueError(
                f"finding must carry a path and a severity, got {finding!r}"
            ) from exc
        if not isinstance(path, str) or not path:
            raise ValueError(f"finding path must be a non-empty string, got {path!r}")
        if _FIELD_SEP in path or _RECORD_SEP in path:
            raise ValueError(
                "finding path must not contain the canonical form's ASCII"
                f" separators, got {path!r}"
            )
        if severity not in SEVERITIES:
            raise ValueError(
                f"finding severity must be one of {SEVERITIES}, got {severity!r}"
            )
        line = finding.get("line")
        if line is None:
            line = _NO_LINE
        elif isinstance(line, bool) or not isinstance(line, int):
            raise ValueError(
                f"finding line must be an integer or absent, got {line!r}"
            )
        elif line < 1:
            raise ValueError(
                f"finding line must be a positive integer or absent, got {line!r}"
            )
        keys.add((path, line, severity))
    return keys


def findings_fingerprint(findings):
    """Hash a review round's `findings` into its stable fingerprint.

    State-model §2: "hash of sorted (path:line:severity) tuples". `findings` is
    the decoded `reviewRounds.findings` list — mappings with `path`, an
    optional `line`, and a `severity`; any other keys are ignored.

    Sorted before hashing, so the order the reviewer happened to emit its
    findings in cannot change the answer: two rounds that raised the same
    complaints fingerprint identically, which is the whole mechanism §6's
    `review_stuck` trip condition reads. A round that found nothing hashes to
    `EMPTY_FINGERPRINT` rather than raising — zero findings is a `pass`, an
    ordinary outcome, not an error.

    Pure. The result is a 64-character sha256 hex digest, sized for
    `reviewRounds.findingsFingerprint`.
    """
    canonical = _RECORD_SEP.join(
        _FIELD_SEP.join((path, str(line), severity))
        for path, line, severity in sorted(_finding_keys(findings))
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def findings_overlap(earlier, later):
    """How much two rounds' findings share, as a fraction in [0.0, 1.0].

    The Jaccard index over the same `(path, line, severity)` keys
    `findings_fingerprint()` hashes: shared keys divided by the keys either
    round raised. Two rounds sharing two of three findings each score
    `2/4 = 0.5` — the shared fraction of everything on the table, not of
    either round alone, so neither a reviewer that drops findings nor one that
    piles new ones on can inflate the number.

    §6 reads a fingerprint match as "the same round twice" and this as the
    softer signal next to it: a review that keeps re-raising most of its
    findings is not converging even when the fingerprints differ. The
    threshold is the supervisor's policy, not this function's.

    Identical inputs always score 1.0, including two rounds that both found
    nothing: the sets are equal, and a `pass` after a `pass` is not the caller's
    stuck-review question. Argument order does not matter — the measure is
    symmetric; the names only say how it is usually read.
    """
    earlier_keys = _finding_keys(earlier)
    later_keys = _finding_keys(later)
    union = earlier_keys | later_keys
    if not union:
        return 1.0
    return len(earlier_keys & later_keys) / len(union)


# §2's `reviewRounds.verdict` union, transcribed from the column's CHECK so a
# caller can map onto it without reading the DDL. The reviewer's own
# vocabulary is a different one (`APPROVE`/`REQUEST_CHANGES`, `PASS`/`FAIL`);
# translating it is the loop's job, not this module's.
ROUND_VERDICTS = ("pass", "changes_requested", "error")


def _document_argument(label, value):
    """`value` as the list a `reviewRounds` document column is written from.

    The contract's arguments are object *arrays*, and `list()` alone does not
    say so: it accepts any iterable, so `findings="prose"` becomes a document
    of five one-character findings and a mapping becomes a document of its
    keys. Both were written past the refusal they were supposed to hit, and
    the renderer can only show them as a row nothing should have stored. So
    the shape is checked before the coercion, and a string, a mapping, or
    anything that is not a sequence is a `ValueError` named for `label`.
    """
    if isinstance(value, (list, tuple)):
        return list(value)
    raise ValueError(
        f"{label} must be a list of objects, got {type(value).__name__}")


def _json_document(label, value):
    """`value` as the JSON text a `reviewRounds` document column stores.

    Strict on both sides of the encoder: a Python value it cannot serialize
    raises `TypeError`, and `allow_nan=False` turns the non-JSON floats it
    would otherwise emit into a `ValueError`. Both become the `ValueError`
    the caller's other refusals are, named for `label`, so a round that
    cannot be written as valid JSON is refused whole rather than stored as
    text the renderer cannot read back.
    """
    try:
        return json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a JSON document: {exc}") from exc


def record_review_round(conn, run_id, round_number, verdict, reviewer_model,
                        findings=(), verification_results=(),
                        started_at=None, ended_at=None):
    """Persist one review round of `run_id` as a row; return its id.

    The write half of the fingerprint helpers above: `findings` is hashed
    into `findingsFingerprint` here rather than by the caller, because §6's
    stuck-review check compares digests across rounds recorded at different
    times, and a caller that computed its own could hash a different
    normalization and make two identical rounds look unlike. Hashing first
    also validates: `findings_fingerprint()` rejects a malformed finding
    before this opens a transaction, so a round is stored whole or not at all.

    `findings` and `verification_results` are the contract's object arrays,
    stored as the JSON documents the schema declares. Both are encoded here,
    before the transaction, for the same reason the fingerprint is: a value
    `json` cannot write (bytes, a set) or writes as something no reader can
    decode back (`NaN`, `Infinity` — legal to the encoder, not to JSON) is a
    `ValueError`, and nothing is stored. So is a value that is not a list at
    all: `"prose"` and `{...}` are iterable, and coercing them would store a
    document of characters or of keys instead of refusing the caller's
    mistake. The renderer reads these columns
    back with `json.loads()`; refusing here is what lets it trust them. A
    round that found nothing is an ordinary `pass` and stores `[]` against
    `EMPTY_FINGERPRINT`.

    `ended_at` defaults to NULL, which is the column's "still running"; a
    caller recording a finished round passes both stamps. `started_at`
    defaults to the clock because the column is NOT NULL and a round being
    recorded has certainly started.

    The run is checked inside the transaction for the module's usual reason: an
    unknown `run_id` is a caller bug and raises `ValueError` rather than a
    foreign-key `IntegrityError` from the driver. `UNIQUE (runId, round)` is
    left to the database — recording the same round twice is a bug this must
    not paper over by overwriting the first record of it.
    """
    if verdict not in ROUND_VERDICTS:
        raise ValueError(
            f"round verdict must be one of {ROUND_VERDICTS}, got {verdict!r}"
        )
    if (isinstance(round_number, bool) or not isinstance(round_number, int)
            or round_number < 1):
        raise ValueError(
            f"round must be a positive integer, got {round_number!r}"
        )
    findings = _document_argument("findings", findings)
    fingerprint = findings_fingerprint(findings)
    findings_json = _json_document("findings", findings)
    results_json = _json_document(
        "verification results",
        _document_argument("verification results", verification_results))
    if started_at is None:
        started_at = int(time.time() * 1000)
    with _transaction(conn):
        if conn.execute(
            "SELECT 1 FROM runs WHERE id = ?", (run_id,)
        ).fetchone() is None:
            raise ValueError(f"no run {run_id}")
        cursor = conn.execute(
            "INSERT INTO reviewRounds (runId, round, verificationResults,"
            " verdict, findings, findingsFingerprint, reviewerModel,"
            " startedAt, endedAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, round_number, results_json, verdict, findings_json,
             fingerprint, reviewer_model, started_at, ended_at),
        )
    return cursor.lastrowid


# --- the supervisor sweep's strike tally --------------------------------------
# A run whose loop has crashed stops heartbeating, but so does one whose host
# is briefly wedged, so liveness is not a single sample: the sweep records what
# it saw and only the second consecutive silent sighting is evidence. The
# counting lives here rather than in the sweep because it is a read-then-write
# over a store table, and two sweeps racing on one target must serialize on it
# the way every other writer in this module does.


def record_strike(conn, run_id, stale, heartbeat, now=None):
    """Record one sweep's liveness sighting of run `run_id`; return its strikes.

    `stale` is the sweep's verdict on this run's heartbeat, not a threshold
    this decides: the sweep owns how old is too old (and 5/5 will make that
    configurable), and this owns only how many sightings in a row say so.

    A silent run's tally goes up by one and the row remembers the sweep that
    last touched it. A run seen alive drops its row and answers 0 -- the
    strikes a sweep counts are consecutive, so one heartbeat clears the count
    rather than leaving a run one old sighting away from tripping forever.

    Which is why `heartbeat` -- the run's `lastHeartbeat`, the timestamp the
    caller's verdict was reached on -- is compared against the `lastSeen` of
    the strike already on file. A sighting is only the *next* consecutive one
    if the run has been silent throughout; a run that answered after the last
    strike was recorded and then went quiet again has proved itself alive in
    between, and starts over at one however few sweeps saw it do so. Counting
    on sightings alone makes the tally consecutive in sweeps rather than in
    silence, and a run heartbeating just slower than the sweep interval trips
    while alive -- exactly the false positive two strikes exist to prevent.

    An unknown `run_id` is a caller bug and raises `ValueError`, as everywhere
    else here. `now` is epoch milliseconds for `lastSeen`, defaulting to the
    clock; a sweep passes its own so every run in one pass is stamped with the
    one time it was taken.
    """
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        if conn.execute(
            "SELECT 1 FROM runs WHERE id = ?", (run_id,)
        ).fetchone() is None:
            raise ValueError(f"no run {run_id}")
        if not stale:
            conn.execute("DELETE FROM sweepStrikes WHERE runId = ?", (run_id,))
            strikes = 0
        else:
            row = conn.execute(
                "SELECT strikes, lastSeen FROM sweepStrikes WHERE runId = ?",
                (run_id,)
            ).fetchone()
            if row is None or heartbeat > row[1]:
                # Nothing on file, or the run answered after what is: either
                # way this is the first sighting of the silence it is in now.
                strikes = 1
            else:
                strikes = row[0] + 1
            conn.execute(
                "INSERT INTO sweepStrikes (runId, strikes, lastSeen)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT (runId) DO UPDATE SET strikes = excluded.strikes,"
                " lastSeen = excluded.lastSeen",
                (run_id, strikes, now),
            )
    return strikes


def record_supervisor_heartbeat(conn, pid, started_at, now=None):
    """Record one completed pass of the supervisor `pid`; return its passes.

    The supervisor is identified by `(pid, started_at)` rather than pid alone
    because pids are reused: a supervisor started tomorrow with yesterday's
    pid is a different watcher, and folding its passes into the old row would
    make the old one look like it never died. The first call inserts the row
    with one pass; every later call bumps `lastBeat` and the count. `now` is
    epoch milliseconds, defaulting to the clock; the loop passes the instant
    its sweep ran so the beat and the sweep it vouches for agree. Every beat
    stamps `host` with this machine's hostname, so a store read elsewhere can
    say which machine the watcher is on.
    """
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        conn.execute(
            "INSERT INTO supervisorHeartbeats"
            " (pid, startedAt, lastBeat, passes, host)"
            " VALUES (?, ?, ?, 1, ?)"
            " ON CONFLICT (pid, startedAt) DO UPDATE SET"
            "   lastBeat = excluded.lastBeat, passes = passes + 1,"
            "   host = excluded.host",
            (pid, started_at, now, socket.gethostname()),
        )
        return conn.execute(
            "SELECT passes FROM supervisorHeartbeats"
            " WHERE pid = ? AND startedAt = ?", (pid, started_at)).fetchone()[0]


def latest_supervisor_heartbeat(conn):
    """The newest supervisor heartbeat, or None when no supervisor has beaten.

    `(pid, started_at, last_beat, passes, host)` for the row whose `lastBeat`
    is most recent: the one supervisor that could still be alive, since any
    other process's row stopped moving before it. `host` is None for a beat
    written before the column existed. Read-only, so `--report` can ask it
    of a store a live supervisor is writing to.
    """
    row = conn.execute(
        "SELECT pid, startedAt, lastBeat, passes, host"
        " FROM supervisorHeartbeats"
        " ORDER BY lastBeat DESC, startedAt DESC LIMIT 1").fetchone()
    return tuple(row) if row is not None else None


if __name__ == "__main__":
    import sys

    if sys.argv[1:] != ["--state-graph"]:
        sys.exit("usage: python3 store.py --state-graph")
    print("\n".join(render_state_graph_section(name, globals()[table])
                    for name, table in STATE_GRAPHS), end="")
