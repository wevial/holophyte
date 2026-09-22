
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
    admission           TEXT NOT NULL DEFAULT 'enabled'
        CHECK (admission IN ('enabled', 'held')),
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
