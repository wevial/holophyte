-- Frozen schema 26, before enum-generated constraints (KO-579).
CREATE TABLE projects (
    id                  INTEGER PRIMARY KEY,
    linearTeamId        TEXT    NOT NULL UNIQUE,
    repoPath            TEXT    NOT NULL,
    defaultBranch       TEXT    NOT NULL,
    autonomyProfile     TEXT    NOT NULL
        CHECK (autonomyProfile IN ('personal', 'shared_low_risk', 'production')),
    highRiskPaths       TEXT    NOT NULL DEFAULT '[]',
    verificationDefault TEXT,
    boardAskedAt        INTEGER,
    launchBackoffUntil  INTEGER,
    launchBackoffReason TEXT,
    activeRunId         INTEGER
        REFERENCES runs (id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE tickets (
    id                   INTEGER PRIMARY KEY,
    projectId            INTEGER NOT NULL REFERENCES projects (id),
    linearIssueId        TEXT    NOT NULL UNIQUE,
    url                  TEXT,
    boardState           TEXT,
    linearIdentifier     TEXT    NOT NULL,
    title                TEXT    NOT NULL,
    body                 TEXT    NOT NULL DEFAULT '',
    status               TEXT    NOT NULL
        CHECK (status IN ('needs_spec', 'ready', 'in_flight', 'blocked_on_deps',
                          'blocked_on_operator', 'merged', 'abandoned')),
    acceptanceCriteria   TEXT    NOT NULL DEFAULT '[]',
    verificationCommands TEXT    NOT NULL DEFAULT '[]',
    timeBoxMs            INTEGER,
    affinity             TEXT    NOT NULL
        CHECK (affinity IN ('any', 'gui', 'headless')),
    dependsOn            TEXT    NOT NULL DEFAULT '[]',
    activeRunId          INTEGER
        REFERENCES runs (id) DEFERRABLE INITIALLY DEFERRED,
    lastRunId            INTEGER
        REFERENCES runs (id) DEFERRABLE INITIALLY DEFERRED,
    blockedQuestion      TEXT,
    splitDepth           INTEGER NOT NULL DEFAULT 0,
    mirroredAt           INTEGER NOT NULL
);
CREATE TABLE runs (
    id                INTEGER PRIMARY KEY,
    ticketId          INTEGER NOT NULL REFERENCES tickets (id),
    projectId         INTEGER NOT NULL REFERENCES projects (id),
    attempt           INTEGER NOT NULL,
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
    lastHeartbeat     INTEGER NOT NULL,
    endedAt           INTEGER,
    reviewRoundCount  INTEGER NOT NULL DEFAULT 0,
    timeBoxMs         INTEGER,
    workingMs         INTEGER,
    workStartedAt     INTEGER,
    ticketSnapshot    TEXT,
    outcome           TEXT
        CHECK (outcome IS NULL
               OR outcome IN ('merged', 'killed', 'abandoned', 'failed', 'rejected')),
    outcomeReason     TEXT,
    mergeSha          TEXT,
    outcomeClass      TEXT    NOT NULL DEFAULT 'work'
        CHECK (outcomeClass IN ('work', 'infra')),
    host              TEXT,
    resumePhase       TEXT
        CHECK (resumePhase IS NULL
               OR resumePhase IN ('claimed', 'working', 'verifying', 'reviewing',
                                  'addressing', 'merge_gate',
                                  'awaiting_merge_approval', 'merging',
                                  'squashing', 'done', 'blocked_on_operator',
                                  'failed', 'killed', 'rejected')),
    candidateSha      TEXT,
    approvedSha       TEXT,
    approvedAt        INTEGER,
    approvedBy        TEXT,
    reviewRoundCap    INTEGER,
    prSeenAt          TEXT,
    prSeenThreads     INTEGER,
    prSeenChecks      TEXT,
    prSeenReview      TEXT,
    UNIQUE (ticketId, attempt)
);
CREATE TABLE reviewRounds (
    id                  INTEGER PRIMARY KEY,
    runId               INTEGER NOT NULL REFERENCES runs (id),
    round               INTEGER NOT NULL,
    verificationResults TEXT    NOT NULL DEFAULT '[]',
    verdict             TEXT    NOT NULL
        CHECK (verdict IN ('pass', 'changes_requested', 'error')),
    findings            TEXT    NOT NULL DEFAULT '[]',
    findingsFingerprint TEXT    NOT NULL,
    reviewerModel       TEXT    NOT NULL,
    startedAt           INTEGER NOT NULL,
    endedAt             INTEGER,
    UNIQUE (runId, round)
);
CREATE TABLE runEvents (
    id      INTEGER PRIMARY KEY,
    runId   INTEGER REFERENCES runs (id),
    projectId INTEGER REFERENCES projects (id),
    seq     INTEGER NOT NULL,
    level   TEXT    NOT NULL CHECK (level IN ('narrative', 'detail')),
    kind    TEXT    NOT NULL,
    summary TEXT    NOT NULL,
    payload TEXT,
    at      INTEGER NOT NULL,
    CHECK (runId IS NOT NULL OR projectId IS NOT NULL),
    UNIQUE (runId, seq)
);
CREATE TABLE sweepStrikes (
    runId    INTEGER PRIMARY KEY REFERENCES runs (id),
    strikes  INTEGER NOT NULL,
    lastSeen INTEGER NOT NULL
);
CREATE TABLE supervisorHeartbeats (
    pid       INTEGER NOT NULL,
    startedAt INTEGER NOT NULL,
    lastBeat  INTEGER NOT NULL,
    passes    INTEGER NOT NULL,
    host      TEXT,
    PRIMARY KEY (pid, startedAt)
);
CREATE TABLE loopRestarts (
    id         INTEGER PRIMARY KEY,
    projectId  INTEGER NOT NULL REFERENCES projects (id),
    sha        TEXT    NOT NULL,
    at         INTEGER NOT NULL,
    returnedAt INTEGER,
    reportedAt INTEGER
);
CREATE TABLE linearDeliveries (
    deliveryId  TEXT    PRIMARY KEY,
    processedAt INTEGER NOT NULL
);
CREATE TABLE ledger (
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
                            'config_edit', 'operator_note', 'migrate')),
    note      TEXT,
    question  TEXT,
    guidance  TEXT,
    at        INTEGER NOT NULL,
    CHECK (runId IS NOT NULL OR projectId IS NOT NULL OR "action" = 'migrate')
);
PRAGMA user_version = 26;
