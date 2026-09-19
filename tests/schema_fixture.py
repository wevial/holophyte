"""Expected store columns, independent of the schema DDL."""

DOCUMENTED_COLUMNS = {
    "projects": {
        "id", "linearTeamId", "repoPath", "defaultBranch", "autonomyProfile",
        "highRiskPaths", "verificationDefault", "activeRunId",
        # Store-owned: when the supervisor's board fallback last asked
        # Linear for the ready listing, so `board_ask_sec` throttles
        # across passes and restarts (KO-434).
        "boardAskedAt", "launchBackoffUntil", "launchBackoffReason",
    },
    "tickets": {
        "id", "projectId", "linearIssueId", "linearIdentifier", "title",
        "status", "acceptanceCriteria", "verificationCommands", "timeBoxMs",
        "affinity", "dependsOn", "activeRunId", "lastRunId", "blockedQuestion",
        "splitDepth", "mirroredAt",
        # Store-owned: the Linear body the claim-time mirror last read, so
        # the daemon serves the contract the run worked from (KO-328).
        "body",
        # Claim-time Linear issue URL for console ticket links (KO-478).
        "url", "boardState",
    },
    "runs": {
        "id", "ticketId", "projectId", "attempt", "phase", "workerId",
        "providerSessionId", "branch", "prUrl", "startedAt", "lastHeartbeat",
        "endedAt", "reviewRoundCount", "outcome", "outcomeReason",
        "workingMs", "workStartedAt",
        # Store-owned: the merge commit a merged run landed on main as, so
        # the ticket-to-commit link is a column and not a grep of git log.
        "mergeSha",
        "candidateSha",
        "approvedSha", "approvedAt", "approvedBy",
        # Store-owned: the review-round cap the loop gave the run, so the
        # console sizes the round timeline by it rather than a constant.
        "reviewRoundCap",
        "prSeenAt",
        "prSeenThreads",
        # Store-owned: the checks rollup and review decision the same read
        # saw, so `/attention`'s `pr_open` item carries them (KO-368).
        "prSeenChecks",
        "prSeenReview",
        # Store-owned, not a documented field: §5 requires a resume to
        # "re-enter the phase it left" and leaves the mechanism to us, so
        # `resume()` reads the parked phase from this column.
        "resumePhase",
        # Store-owned too: the ticket's estimate as it stood at the claim, so
        # a finished run's estimate-vs-actual does not move when the ticket's
        # own `timeBoxMs` is later re-mirrored.
        "timeBoxMs",
        # Store-owned as well: the ticket's contract frozen at the claim, so
        # the merge gate can tell a body edited mid-run from the one the run
        # was worked to.
        "ticketSnapshot",
        # Store-owned: whether a failure is evidence about the ticket
        # (`work`) or about the factory's own plumbing (`infra`), so the
        # escalation count can leave the second kind out.
        "outcomeClass",
        # Store-owned: the hostname that claimed the run, so a store read on
        # another machine can say where each live run is executing.
        "host",
    },
    "ledger": {
        "id", "runId", "ticketId", "at", "kind", "text", "source",
    },
    "reviewRounds": {
        "id", "runId", "round", "verificationResults", "verdict", "findings",
        "findingsFingerprint", "reviewerModel", "startedAt", "endedAt",
    },
    "runEvents": {
        "id", "runId", "projectId", "seq", "level", "kind", "summary", "payload", "at",
    },
    "interventions": {
        "id", "runId", "projectId", "source", "trigger", "action", "question",
        "guidance", "at", "note",
    },
    "linearDeliveries": {"deliveryId", "processedAt"},
    # Store-owned, not a documented table: the supervisor sweep's per-run
    # strike tally, which exists because "silent on two consecutive sweeps"
    # has to survive between two sweep invocations.
    "sweepStrikes": {"runId", "strikes", "lastSeen"},
    # Store-owned as well: one row per `--supervise` process, bumped on every
    # pass, so a reader can tell a live watcher from a dead one.
    "supervisorHeartbeats": {"pid", "startedAt", "lastBeat", "passes", "host"},
    # And one row per self-merge re-exec of the loop, so the sweep can tell a
    # restart that came back from one that died in the exec.
    "loopRestarts": {"id", "projectId", "sha", "at", "returnedAt",
                     "reportedAt"},
}
