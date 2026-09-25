-- Frozen schema 36, before ticket revisions, notes and board columns (KO-733).
CREATE TABLE sweepStrikes (
    runId    INTEGER PRIMARY KEY REFERENCES runs (id),
    strikes  INTEGER NOT NULL,
    lastSeen INTEGER NOT NULL  -- when the latest strike was recorded, so a
                               -- heartbeat newer than it restarts the count
);

CREATE TABLE supervisorHeartbeats (
    pid       INTEGER NOT NULL,
    startedAt INTEGER NOT NULL,  -- when this supervisor process took the lock
    lastBeat  INTEGER NOT NULL,  -- when it last completed a pass
    passes    INTEGER NOT NULL,  -- how many passes it has completed
    host      TEXT,              -- the machine the supervisor runs on
    PRIMARY KEY (pid, startedAt)
);

CREATE TABLE loopRestarts (
    id         INTEGER PRIMARY KEY,
    projectId  INTEGER NOT NULL REFERENCES projects (id),
    sha        TEXT    NOT NULL,  -- the merged commit the loop re-executed from
    at         INTEGER NOT NULL,  -- when the exec was about to happen
    returnedAt INTEGER,           -- when a loop next wrote its exit note
    reportedAt INTEGER            -- when the sweep reported it unreturned
);

CREATE TABLE linearDeliveries (
    deliveryId  TEXT    PRIMARY KEY,
    processedAt INTEGER NOT NULL
);

CREATE TABLE interventions (
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
                            'config_edit', 'operator_note', 'migrate', 'hold', 'release_hold', 'register_project', 'disable', 'pause', 'abort', 'abort_close')),
    note      TEXT,  -- store-level migration evidence as JSON
    question  TEXT,  -- for redirect
    guidance  TEXT,  -- human answer, only when the run was blocked_on_operator
    at        INTEGER NOT NULL,
    CHECK (runId IS NOT NULL OR projectId IS NOT NULL OR "action" = 'migrate')
);

CREATE TABLE "projects" (
    id                  INTEGER PRIMARY KEY,
    linearTeamId        TEXT    NOT NULL UNIQUE,  -- maps 1:1 to a Linear team
    repoPath            TEXT    NOT NULL,
    defaultBranch       TEXT    NOT NULL,
    autonomyProfile     TEXT    NOT NULL
        CHECK (autonomyProfile IN ('personal', 'shared_low_risk', 'production')),
    highRiskPaths       TEXT    NOT NULL DEFAULT '[]',  -- JSON string[] of globs
    verificationDefault TEXT,
    admission           TEXT NOT NULL DEFAULT 'enabled'
        CHECK (admission IN ('enabled', 'held', 'disabled')),
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

CREATE TABLE "tickets" (
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

CREATE TABLE "runs" (
    id                INTEGER PRIMARY KEY,
    ticketId          INTEGER NOT NULL REFERENCES tickets (id),
    projectId         INTEGER NOT NULL REFERENCES projects (id),
    attempt           INTEGER NOT NULL,  -- 1-based
    phase             TEXT    NOT NULL
        CHECK (phase IN ('claimed', 'working', 'verifying', 'reviewing',
                         'addressing', 'merge_gate', 'awaiting_merge_approval',
                         'merging', 'squashing', 'done', 'blocked_on_operator',
                         'failed', 'killed', 'rejected', 'paused')),
    workerId          TEXT,
    providerSessionId TEXT,
    branch            TEXT,
    prUrl             TEXT,
    parkKind          TEXT CHECK (parkKind IN ('pull_request', 'pull_request_closed', 'thread', 'fix_declined', 'merge_lock', 'question', 'not_reproduced')),
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
        CHECK (outcome IS NULL
               OR outcome IN ('merged', 'killed', 'abandoned', 'failed', 'rejected', 'paused')),
    outcomeReason     TEXT,
    failureKind TEXT
        CHECK (failureKind IN ('verify', 'review_route', 'fix_no_progress', 'no_commits', 'budget', 'merge_lock', 'infra', 'swept', 'unclassified')),
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
        CHECK (resumePhase IS NULL
               OR resumePhase IN ('claimed', 'working', 'verifying', 'reviewing',
                                  'addressing', 'merge_gate',
                                  'awaiting_merge_approval', 'merging',
                                  'squashing', 'done', 'blocked_on_operator',
                                  'failed', 'killed', 'rejected', 'paused')),
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

CREATE TABLE "reviewRounds" (
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

CREATE TABLE "runEvents" (
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

CREATE TABLE "ledger" (
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

CREATE INDEX runs_ticketId ON runs (ticketId);

CREATE INDEX reviewRounds_runId ON reviewRounds (runId);

CREATE INDEX runEvents_runId ON runEvents (runId);

CREATE INDEX ledger_runId ON ledger (runId);
