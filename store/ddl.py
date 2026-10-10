from __future__ import annotations

from . import enums as _enums

# Every statement is IF NOT EXISTS, so a column added here also needs its
# `ADDED_COLUMNS` entry in schema.py. `trigger` and `action` are SQLite
# keywords, quoted rather than renamed away from the contract's names.
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
        REFERENCES runs (id) DEFERRABLE INITIALLY DEFERRED,
    -- The last ticket number the store handed out for this project, for a
    -- board the store owns (KO-733).
    ticketSeq           INTEGER NOT NULL DEFAULT 0
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
    mirroredAt           INTEGER NOT NULL,
    -- The board-owned fields and their push to a board (KO-733): the
    -- column, priority and JSON labels a board shows, when the ticket was
    -- filed and last changed there, the `ticketRevisions` number its
    -- current fields are, and a pending push's state, origin and time.
    -- `goneSince` is when the board stopped listing the ticket.
    boardColumn          TEXT
        {_enums.check_clause('boardColumn', _enums.BoardColumn)},
    priority             INTEGER,
    labels               TEXT    NOT NULL DEFAULT '[]',  -- JSON string[]
    filedAt              INTEGER,
    boardUpdatedAt       INTEGER,
    revision             INTEGER NOT NULL DEFAULT 0,     -- 0 = none recorded
    pushState            TEXT,
    pushFrom             TEXT,
    pushAt               INTEGER,
    goneSince            INTEGER,
    parentTicketId       INTEGER REFERENCES tickets (id)  -- the story it serves
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
    -- The `ticketRevisions` number the run was claimed at (KO-733), NULL
    -- on a run claimed before anything recorded one.
    revision          INTEGER,
    storyGeneration   INTEGER,
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

-- ticketRevisions: each version of a ticket's board-owned fields, numbered
-- from 1 per ticket (KO-733), and `tickets.revision` names the current one.
-- `boardColumn`, not `column`: COLUMN is an SQLite keyword.
CREATE TABLE IF NOT EXISTS ticketRevisions (
    ticketId    INTEGER NOT NULL REFERENCES tickets (id),
    revision    INTEGER NOT NULL,
    at          INTEGER NOT NULL,
    author      TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    body        TEXT    NOT NULL DEFAULT '',
    priority    INTEGER,
    labels      TEXT    NOT NULL DEFAULT '[]',  -- JSON string[]
    boardColumn TEXT
        {_enums.check_clause('boardColumn', _enums.BoardColumn)},
    PRIMARY KEY (ticketId, revision)
);

-- ticketNotes: a note on a ticket and its post to the board (KO-733).
-- `dedupKey` makes a retried write collide instead of posting twice;
-- `postedAt` and `postError` record the post's outcome.
CREATE TABLE IF NOT EXISTS ticketNotes (
    id        INTEGER PRIMARY KEY,
    ticketId  INTEGER NOT NULL REFERENCES tickets (id),
    runId     INTEGER REFERENCES runs (id),
    at        INTEGER NOT NULL,
    author    TEXT    NOT NULL,
    kind      TEXT    NOT NULL,
    dedupKey  TEXT,
    text      TEXT    NOT NULL,
    postedAt  INTEGER,
    postError TEXT,
    UNIQUE (ticketId, dedupKey)
);

-- gapLayers: where the lesson of a gap the operator found landed on the
-- correction ladder, append-only; a ticket's highest id is its current layer.
-- `carriedBy` names the ticket carrying the lesson when it is not the gap's.
CREATE TABLE IF NOT EXISTS gapLayers (
    id        INTEGER PRIMARY KEY,
    ticketId  INTEGER NOT NULL REFERENCES tickets (id),
    layer     TEXT    NOT NULL
        {_enums.check_clause('layer', _enums.GapLayer)},
    note      TEXT    NOT NULL,
    carriedBy TEXT,
    author    TEXT    NOT NULL,
    at        INTEGER NOT NULL,
    foundBy   TEXT    NOT NULL DEFAULT 'operator'
        {_enums.check_clause('foundBy', _enums.GapFinder)}
);

-- stories: a parent ticket whose children serve one outcome, closed when its
-- witnesses pass on main's tip.
CREATE TABLE IF NOT EXISTS stories (
    ticketId         INTEGER PRIMARY KEY REFERENCES tickets (id),
    state            TEXT    NOT NULL
        {_enums.check_clause('state', _enums.StoryState)},
    generation       INTEGER NOT NULL DEFAULT 0,
    standingOrders   TEXT    NOT NULL DEFAULT '[]',  -- JSON string[]
    approvedRevision INTEGER,
    approvedPlan     TEXT,
    approvedBy       TEXT,
    approvedAt       INTEGER,
    closedSha        TEXT,
    closedAt         INTEGER
);

-- storyWitnesses: a story's acceptance witnesses, one per criterion key.
CREATE TABLE IF NOT EXISTS storyWitnesses (
    storyId     INTEGER NOT NULL REFERENCES stories (ticketId),
    key         TEXT    NOT NULL,
    criterion   TEXT    NOT NULL,
    file        TEXT    NOT NULL,
    command     TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    sourceHash  TEXT    NOT NULL,
    completedBy INTEGER REFERENCES tickets (id),
    PRIMARY KEY (storyId, key)
);

-- storyChildren: a child ticket's role toward a story's witness.
CREATE TABLE IF NOT EXISTS storyChildren (
    ticketId   INTEGER NOT NULL REFERENCES tickets (id),
    witnessKey TEXT    NOT NULL DEFAULT '',
    storyId    INTEGER NOT NULL REFERENCES stories (ticketId),
    role       TEXT    NOT NULL
        {_enums.check_clause('role', _enums.ChildRole)},
    PRIMARY KEY (ticketId, witnessKey)
);

-- witnessResults: each run of a story's witness against a main sha.
CREATE TABLE IF NOT EXISTS witnessResults (
    id           INTEGER PRIMARY KEY,
    storyId      INTEGER NOT NULL REFERENCES stories (ticketId),
    witnessKey   TEXT    NOT NULL,
    mainSha      TEXT    NOT NULL,
    verdict      TEXT    NOT NULL
        {_enums.check_clause('verdict', _enums.WitnessVerdict)},
    redKind      TEXT {_enums.check_clause('redKind', _enums.RedKind)},
    verifier     TEXT    NOT NULL
        {_enums.check_clause('verifier', _enums.WitnessVerifier)},
    fileHash     TEXT,
    evidencePath TEXT,
    seconds      REAL,
    at           INTEGER NOT NULL
);

-- storyDecisions: a question a story put to the operator and its answer.
CREATE TABLE IF NOT EXISTS storyDecisions (
    id            INTEGER PRIMARY KEY,
    storyId       INTEGER NOT NULL REFERENCES stories (ticketId),
    ticketId      INTEGER REFERENCES tickets (id),
    kind          TEXT    NOT NULL
        {_enums.check_clause('kind', _enums.DecisionKind)},
    question      TEXT    NOT NULL,
    options       TEXT    NOT NULL,  -- JSON
    defaultOption TEXT    NOT NULL,
    answer        TEXT,
    answeredBy    TEXT,
    answeredAt    INTEGER,
    at            INTEGER NOT NULL
);

-- followUps: a fix commit's FOLLOW_UP line, pending until its run merges; a
-- settled guardrail row is a findings-ledger entry.
CREATE TABLE IF NOT EXISTS followUps (
    id          INTEGER PRIMARY KEY,
    runId       INTEGER NOT NULL REFERENCES runs (id),
    ticketId    INTEGER NOT NULL REFERENCES tickets (id),
    commitSha   TEXT    NOT NULL,
    kind        TEXT    NOT NULL
        {_enums.check_clause('kind', _enums.FollowUpKind)},
    kindGiven   INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    path        TEXT,
    line        INTEGER,
    fingerprint TEXT    NOT NULL,
    createdAt   INTEGER NOT NULL,
    settledAt   INTEGER,
    filedAs     TEXT,
    duplicateOf TEXT,
    error       TEXT,
    UNIQUE (runId, fingerprint)
);

-- storyProposals: a story child's feature follow-up proposed as a new child,
-- outside the plan until a decision accepts it.
CREATE TABLE IF NOT EXISTS storyProposals (
    id            INTEGER PRIMARY KEY,
    storyId       INTEGER NOT NULL REFERENCES stories (ticketId),
    followUpId    INTEGER NOT NULL UNIQUE REFERENCES followUps (id),
    raisedBy      INTEGER NOT NULL REFERENCES tickets (id),
    title         TEXT    NOT NULL,
    body          TEXT    NOT NULL,
    state         TEXT    NOT NULL
        {_enums.check_clause('state', _enums.ProposalState)},
    childTicketId INTEGER REFERENCES tickets (id),
    decidedBy     TEXT,
    decidedAt     INTEGER,
    at            INTEGER NOT NULL
);

-- steerNotes: a maintainer's note on a ticket, an amendment to its contract
-- or one implement turn's hint; consumption is recorded beside the note.
CREATE TABLE IF NOT EXISTS steerNotes (
    id             INTEGER PRIMARY KEY,
    ticketId       INTEGER NOT NULL REFERENCES tickets (id),
    runId          INTEGER REFERENCES runs (id),
    kind           TEXT    NOT NULL
        {_enums.check_clause('kind', _enums.SteerKind)},
    note           TEXT    NOT NULL,
    author         TEXT    NOT NULL,
    at             INTEGER NOT NULL,
    interventionId INTEGER NOT NULL REFERENCES interventions (id),
    eventId        INTEGER REFERENCES runEvents (id),
    consumedBy     INTEGER REFERENCES runs (id),
    consumedAt     INTEGER
);
"""


# Outside SCHEMA: `_widen_interventions_action()` rebuilds an older store's
# table from this exact text, so a migrated store and a fresh one agree.
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


INDEXES = """
CREATE INDEX IF NOT EXISTS runs_ticketId ON runs (ticketId);
CREATE INDEX IF NOT EXISTS reviewRounds_runId ON reviewRounds (runId);
CREATE INDEX IF NOT EXISTS runEvents_runId ON runEvents (runId);
CREATE INDEX IF NOT EXISTS ledger_runId ON ledger (runId);
CREATE INDEX IF NOT EXISTS tickets_parentTicketId ON tickets (parentTicketId);
"""
