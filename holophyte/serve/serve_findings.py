from __future__ import annotations

import store.read
from holophyte.serve.serve_runs import (
    advisories,
    ledger_entry,
    no_store,
    parse_limit,
    parse_since,
    run_rounds,
)
from store.follow_ups import follow_ups_since

FINDINGS_LIMIT = 100
FINDINGS_CAP = 500
ADJUDICATION_KINDS = ("round", "adjudication")


def findings(project, query):
    try:
        since = parse_since(query)
        limit = parse_limit(query, default=FINDINGS_LIMIT, cap=FINDINGS_CAP)
    except ValueError as bad:
        return 400, {"error": str(bad)}
    if not project.store_path.exists():
        return 503, no_store(project)
    conn = store.read.open_readonly(project.store_path)
    try:
        run_ids = [run_id for (run_id,) in conn.execute(
            "SELECT runId FROM reviewRounds WHERE endedAt >= ?"
            " GROUP BY runId ORDER BY max(endedAt) DESC, runId DESC LIMIT ?",
            (since, limit))]
        runs = [window_run(conn, project, run_id) for run_id in run_ids]
        follow_ups = [follow_up_body(row)
                      for row in follow_ups_since(conn, since)]
    finally:
        conn.close()
    return 200, {"since": since, "limit": limit, "runs": runs,
                 "follow_ups": follow_ups}


def window_run(conn, project, run_id):
    run = store.read.run_detail(conn, run_id)
    return {"run": run.id, "ticket": run.linearIdentifier, "title": run.title,
            "phase": run.phase, "outcome": run.outcome,
            "ended_ms": run.endedAt, "pr_url": run.prUrl,
            "rounds": run_rounds(conn, project, run.id),
            "ledger": [ledger_entry(entry, {})
                       for entry in store.read.ledger(conn, run.id)
                       if entry.kind in ADJUDICATION_KINDS],
            "advisories": advisories(store.read.narrative_events(conn, run.id))}


def follow_up_body(row):
    return {"id": row.id, "run": row.runId, "ticket": row.ticket,
            "kind": row.kind, "text": row.text, "path": row.path,
            "line": row.line, "created_ms": row.createdAt,
            "settled_ms": row.settledAt, "state": row.state,
            "filed_as": row.filedAs, "duplicate_of": row.duplicateOf,
            "error": row.error}
