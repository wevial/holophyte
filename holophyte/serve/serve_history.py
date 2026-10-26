from __future__ import annotations

import dataclasses
from time import time

import store.read
from holophyte.serve.serve_runs import (
    INTEGER,
    SQLITE_MAX_DIGITS,
    SQLITE_MAX_INT,
    commit_url,
    json_host,
    no_store,
    origin_web_url,
    parse_filter,
    parse_limit,
    parse_since,
    parse_until,
    served_round_caps,
)
from store.run_reads import (
    FAILURE_KINDS,
    HISTORY_ORDERS,
    HISTORY_SORTS,
    STATE_GROUPS,
    RunQuery,
)
from store.working import effective_work

HISTORY_LIMIT = 200
HISTORY_CAP = 5000


def parse_offset(query):
    text = parse_filter(query, "offset")
    if text is None:
        return 0
    digits = text.lstrip("-").lstrip("0")
    if not INTEGER.fullmatch(text) or (text.startswith("-") and digits):
        raise ValueError(f"offset must be a non-negative integer, got {text!r}")
    if len(digits) > SQLITE_MAX_DIGITS:
        return SQLITE_MAX_INT
    return min(int(digits or "0"), SQLITE_MAX_INT)


def parse_list(query, name, allowed=None):
    text = parse_filter(query, name)
    if text is None or (allowed is None and not text):
        return ()
    values = tuple(text.split(","))
    for value in values:
        if allowed is not None and value not in allowed:
            raise ValueError(f"{name} must be a comma list of"
                             f" {', '.join(allowed)}, got {value!r}")
    return values


def parse_query_text(query):
    text = parse_filter(query, "q")
    return text if text and text.strip() else None


def parse_history_query(query):
    return RunQuery(
        offset=parse_offset(query),
        sort=parse_filter(query, "sort", HISTORY_SORTS) or "started",
        order=parse_filter(query, "order", HISTORY_ORDERS) or "desc",
        q=parse_query_text(query),
        states=parse_list(query, "state", STATE_GROUPS),
        failures=parse_list(query, "failure", FAILURE_KINDS),
        implementers=parse_list(query, "implementer"))


def history(project, query, now=None):
    try:
        since = parse_since(query)
        until = parse_until(query)
        if until is not None and until <= since:
            raise ValueError("until must be after since")
        limit = parse_limit(query, default=HISTORY_LIMIT, cap=HISTORY_CAP)
        wanted = parse_history_query(query)
    except ValueError as bad:
        return 400, {"error": str(bad)}
    if not project.store_path.exists():
        return 503, no_store(project)
    now = int(time() * 1000) if now is None else now
    wanted = dataclasses.replace(wanted, now=now)
    conn = store.read.open_readonly(project.store_path)
    try:
        runs, total = store.read.recent_runs(conn, since, limit, until,
                                             query=wanted)
        counts = store.read.run_counts(conn, since, until, wanted)
        labels = store.read.implementer_labels(conn)
        caps = served_round_caps(conn, project, [run.id for run in runs])
    finally:
        conn.close()
    origin = (origin_web_url(project) if any(run.mergeSha for run in runs)
              else None)
    return 200, {"rows": [history_row(project, run, now, caps[run.id], origin)
                          for run in runs],
                 "since": since, "until": until, "limit": limit,
                 "offset": wanted.offset, "sort": wanted.sort,
                 "order": wanted.order,
                 "truncated": total > wanted.offset + limit, "total": total,
                 "counts": counts, "implementers": labels, "now": now}


def history_row(project, run, now, max_rounds, origin):
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
            "max_rounds": max_rounds,
            "findings": run.findingCount, "merge_sha": run.mergeSha,
            "commit_url": commit_url(project, run.mergeSha, origin),
            "additions": run.prSeenAdditions,
            "deletions": run.prSeenDeletions,
            "resumed_as": run.resumedAs,
            "implementer": run.implementer,
            "pr_url": run.prUrl, "host": json_host(project, run.host)}
