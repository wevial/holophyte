"""store: the v2 durable state store, one WAL-mode SQLite file.

Local-first by resolved decision (2026-08-22): all loop state lives behind
this one module so a hosted backend later is a driver swap, not a loop
rewrite. Stdlib ``sqlite3`` only.

The API so far is ``open()`` and ``init()`` for the schema, ``claim()``
for the per-project lease, ``with_delivery()`` for webhook idempotency,
``mirror_ticket()``/``transition()`` for ticket status,
``pickable()``/``next_pickable()`` for the pickability predicate,
``resume()`` for the resume guidance invariant, and
``findings_fingerprint()``/``findings_overlap()`` for stuck-review
detection.

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
        "resumePhase",
        "resumePhase TEXT"
        " CHECK (resumePhase IS NULL"
        " OR resumePhase IN ('claimed', 'working', 'verifying', 'reviewing',"
        "                    'addressing', 'merge_gate',"
        "                    'awaiting_merge_approval', 'merging', 'squashing',"
        "                    'done', 'blocked_on_operator', 'failed',"
        "                    'killed'))",
    ),
)


def init(conn):
    """Create every table the state model defines, if absent, and migrate.

    Two steps, because `CREATE TABLE IF NOT EXISTS` alone would only ever
    bootstrap an empty file: the tables are created, then every `ADDED_COLUMNS`
    entry missing from an existing table is added. That second step is what
    carries a store created by an earlier version of this module forward
    instead of leaving it one column short of the code that reads it.

    Idempotent: safe to call on an already-initialized database, where it
    creates nothing, adds nothing and touches no rows. Not a downgrade path —
    an older module opening a newer store sees columns it does not know about,
    which is harmless, while the reverse is what this repairs.
    """
    conn.executescript(SCHEMA)
    for table, column, ddl in ADDED_COLUMNS:
        # A misspelled table name leaves `columns` empty and the ALTER then
        # raises `no such table`, which is the loud failure this should be.
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    conn.commit()


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


# The §3 status diagram, transcribed edge for edge:
#
#     needs_spec → ready → in_flight → merged
#                     ↕                  ↘
#              blocked_on_deps      abandoned
#                     ↕
#             blocked_on_operator
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
# A status is not in its own set: `ready → ready` is refused like any other
# non-edge, so a no-op status write cannot pass for a real transition.
TICKET_TRANSITIONS = {
    "needs_spec": frozenset({"ready"}),
    "ready": frozenset({"in_flight", "blocked_on_deps"}),
    "in_flight": frozenset({"merged", "abandoned"}),
    "blocked_on_deps": frozenset({"ready", "blocked_on_operator"}),
    "blocked_on_operator": frozenset({"blocked_on_deps"}),
    "merged": frozenset(),
    "abandoned": frozenset(),
}

# Derived, not re-typed, so the enum cannot drift from the transition table.
# It must still match the `tickets.status` CHECK in SCHEMA above; the tests
# assert that against the database rather than trusting the agreement.
TICKET_STATUSES = tuple(TICKET_TRANSITIONS)


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
    depends_on=(),
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
    alone, with one exception: `needs_spec → ready` once the body finally
    carries both lists. §1 gives Holophyte the in-flight substate, so a mirror
    push may not drag a running ticket backwards, and the promotion is the one
    edge of the §3 diagram a mirror can walk on its own — every other status
    change is somebody's decision and goes through `transition()`. In
    particular `blocked_on_deps → ready` is *not* taken here: it is the
    dependency resolver's call, not a side effect of a body edit.

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
    depends = _json_list("depends_on", depends_on)
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
                    depends, now,
                ),
            ).lastrowid
        else:
            ticket_id, status = row
            if status == "needs_spec" and derived == "ready":
                status = "ready"
            conn.execute(
                "UPDATE tickets SET linearIdentifier = ?, title = ?,"
                " status = ?, acceptanceCriteria = ?, verificationCommands = ?,"
                " timeBoxMs = ?, affinity = ?, dependsOn = ?, mirroredAt = ?"
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
RESUMABLE_PHASES = frozenset(
    {"failed", "working", "verifying", "reviewing", "addressing",
     "blocked_on_operator"}
)


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


# §2's severity union, transcribed. Checked here because `reviewRounds.findings`
# is a JSON document rather than rows, so no CHECK constraint stands behind it:
# this helper is the only place a severity typo can be caught before it changes
# a fingerprint and, through it, whether the review looks converged.
SEVERITIES = ("p0", "p1", "p2", "nit")

# The canonical record's separators. §2 writes the key as `path:line:severity`,
# but a colon is a legal character in a path, so joining on one would let
# `{"path": "a:1", "line": 2}` and `{"path": "a", "line": ...}` collide into
# the same record. The ASCII separators cannot occur in a path a reviewer cites,
# so the encoding stays unambiguous without escaping anything.
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
    severity outside §2's union, or citing a line below 1 — a fingerprint over
    malformed findings would be a number that compares fine and means nothing.
    Lines are 1-based, so a 0 or a negative is junk either way; rejecting it
    also keeps `_NO_LINE` unreachable as an explicit value.
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
