"""store: the v2 durable state store, one WAL-mode SQLite file.

Local-first by resolved decision (2026-08-22): all loop state lives behind
this one module so a hosted backend later is a driver swap, not a loop
rewrite. Stdlib ``sqlite3`` only.

The API so far is ``open()`` and ``init()`` for the schema, ``claim()``
for the per-project lease, and ``with_delivery()`` for webhook idempotency.
The remaining query helpers (pickability, status transitions, resume,
fingerprints) belong to the later tickets in this chain.

Conventions, fixed here for every later ticket to follow:

* **camelCase** table and column names, matching the field names in the
  state model verbatim. The table names are camelCase in the contract
  (``reviewRounds``, ``runEvents``, ``linearDeliveries``), so the columns
  follow rather than splitting the file across two casings.
* **List-typed fields are JSON text.** SQLite has no array type, so the
  contract's ``string[]`` and object-array fields are stored as a JSON
  document defaulting to ``'[]'``. Encoding/decoding is a later ticket's
  concern; the schema only pins the storage.
* **Optional (``?``) fields are nullable; everything else is NOT NULL.**
* **Union types become CHECK constraints**, so an unknown status, phase or
  verdict is rejected by the database rather than by a caller who
  remembered to look.
* **Rows are keyed by a synthetic ``id INTEGER PRIMARY KEY``**, standing in
  for the contract's Convex-shaped ``Id<table>`` references.

Contract source: docs/v2/state-model.md §1-§2, plus the per-project lease
column from §7. That document is deliberately gitignored, so the sections
are cited here instead of vendored.
"""
from __future__ import annotations

import collections
import sqlite3
import time

# One statement per table, in dependency order where it matters. Every
# statement is IF NOT EXISTS, which is the whole of init()'s idempotency:
# re-running it on a populated database is a no-op, not a rebuild.
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
    dependsOn            TEXT    NOT NULL DEFAULT '[]',  -- JSON string[] of linearIssueIds
    activeRunId          INTEGER
        REFERENCES runs (id) DEFERRABLE INITIALLY DEFERRED,
    lastRunId            INTEGER
        REFERENCES runs (id) DEFERRABLE INITIALLY DEFERRED,
    blockedQuestion      TEXT,                           -- set when status = blocked_on_operator
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
    outcome           TEXT
        CHECK (outcome IS NULL
               OR outcome IN ('merged', 'killed', 'abandoned', 'failed')),
    outcomeReason     TEXT,
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

-- interventions: supervisor/human actions on a run (state-model §2). Kept
-- out of runEvents because these are queryable decisions, not log lines.
CREATE TABLE IF NOT EXISTS interventions (
    id        INTEGER PRIMARY KEY,
    runId     INTEGER NOT NULL REFERENCES runs (id),
    source    TEXT    NOT NULL CHECK (source IN ('supervisor', 'human')),
    "trigger" TEXT    NOT NULL
        CHECK ("trigger" IN ('time_box', 'off_criteria', 'looping',
                             'review_stuck', 'linear_cancelled', 'manual')),
    "action"  TEXT    NOT NULL
        CHECK ("action" IN ('redirect', 'kill', 'extend_time_box', 'resume')),
    question  TEXT,  -- for redirect
    guidance  TEXT,  -- human answer, only when the run was blocked_on_operator
    at        INTEGER NOT NULL
);

-- linearDeliveries: webhook idempotency (state-model §1). The delivery id is
-- the primary key, so a replayed delivery collides instead of re-running its
-- effect.
CREATE TABLE IF NOT EXISTS linearDeliveries (
    deliveryId  TEXT    PRIMARY KEY,
    processedAt INTEGER NOT NULL
);
"""


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


def init(conn):
    """Create every table the state model defines, if absent.

    Idempotent: safe to call on an already-initialized database, where it
    creates nothing and touches no rows.
    """
    conn.executescript(SCHEMA)
    conn.commit()


class ClaimConflict(Exception):
    """A claim was refused and nothing was written.

    The typed failure of `claim()`: either the project already has an active
    run — v0's single-threading rule (state-model §7), so this caller does not
    get to start another one — or the ticket does not belong to the project
    whose lease was asked for. Both leave every table exactly as it was.
    """


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
        run_id = conn.execute(
            "INSERT INTO runs"
            " (ticketId, projectId, attempt, phase, startedAt, lastHeartbeat)"
            " VALUES (?, ?, ?, 'claimed', ?, ?)",
            (ticket_id, project_id, prior + 1, now, now),
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
