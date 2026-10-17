from __future__ import annotations

from time import time

import store.read
from holophyte.loop.runs import MAX_ROUNDS
from holophyte.serve.serve_runs import (
    json_host,
    no_store,
    parse_limit,
    parse_since,
    parse_until,
)
from store.working import effective_work

HISTORY_LIMIT = 200
HISTORY_CAP = 500


def history(project, query, now=None):
    try:
        since = parse_since(query)
        until = parse_until(query)
        if until is not None and until <= since:
            raise ValueError("until must be after since")
        limit = parse_limit(query, default=HISTORY_LIMIT, cap=HISTORY_CAP)
    except ValueError as bad:
        return 400, {"error": str(bad)}
    if not project.store_path.exists():
        return 503, no_store(project)
    now = int(time() * 1000) if now is None else now
    conn = store.read.open_readonly(project.store_path)
    try:
        runs, total = store.read.recent_runs(conn, since, limit, until)
    finally:
        conn.close()
    return 200, {"rows": [history_row(project, run, now)
                          for run in runs],
                 "since": since, "until": until, "limit": limit,
                 "truncated": total > limit, "total": total, "now": now}


def history_row(project, run, now):
    live = run.endedAt is None
    return {"id": run.id, "ticket": run.linearIdentifier,
            "ticket_url": run.ticketUrl, "title": run.title,
            "attempt": run.attempt, "phase": run.phase,
            "outcome": run.outcome,
            "outcome_reason": (run.outcomeReason[:400]
                               if run.outcomeReason is not None else None),
            "failure_kind": run.failureKind, "park_kind": run.parkKind,
            "started_ms": run.startedAt, "ended_ms": run.endedAt,
            "elapsed_ms": (now if live else run.endedAt) - run.startedAt,
            "estimate_ms": run.timeBoxMs,
            "working_ms": effective_work(run, now if live else run.endedAt),
            "heartbeat_age_ms": now - run.lastHeartbeat if live else None,
            "rounds": run.reviewRoundCount,
            "max_rounds": run.reviewRoundCap or MAX_ROUNDS,
            "pr_url": run.prUrl, "host": json_host(project, run.host)}
