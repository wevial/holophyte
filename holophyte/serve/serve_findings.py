from __future__ import annotations

import json

import store.read
from holophyte.host.supervisor import (
    CRITERION_STUCK,
    REVIEW_STUCK,
    SWEEP_EVENT,
    criteria_stuck,
)
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
CONTRADICTION = "contradiction"
SWEEP_ACTED = "; failing the run and releasing its leases"


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
        defects = ticket_defects(conn, since, limit)
    finally:
        conn.close()
    return 200, {"since": since, "limit": limit, "runs": runs,
                 "follow_ups": follow_ups, "ticket_defects": defects}


def ticket_defects(conn, since, limit):
    stops = conn.execute(
        "SELECT e.runId, e.kind, e.summary, e.payload, e.at, r.ticketId,"
        " r.revision, t.linearIdentifier FROM runs r"
        " CROSS JOIN runEvents e ON e.runId = r.id"
        " JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.revision IS NOT NULL AND EXISTS (SELECT 1 FROM"
        " ticketRevisions v WHERE v.ticketId = r.ticketId"
        " AND v.revision > r.revision)"
        " AND e.kind IN (?, ?) AND e.at >= ?"
        " ORDER BY e.at DESC, e.id DESC", (CONTRADICTION, SWEEP_EVENT, since))
    defects, seen = [], set()
    for run_id, event, summary, payload, at, ticket_id, claimed, ticket in stops:
        kind = stop_kind(event, summary)
        if kind is None or run_id in seen:
            continue
        seen.add(run_id)
        revised = revision_after(conn, run_id, ticket_id, claimed, at)
        if revised is None:
            continue
        defects.append({
            "ticket": ticket, "run": run_id, "kind": kind,
            "criteria": stopped_criteria(conn, run_id, kind, payload),
            "reason": summary, "at": at, "revision": revised[0],
            "revised_ms": revised[1],
            "requeue_note": requeue_note(conn, run_id)})
        if len(defects) >= limit:
            break
    return defects


def stop_kind(event, summary):
    if event == CONTRADICTION:
        return CONTRADICTION
    for kind in (CRITERION_STUCK, REVIEW_STUCK):
        if (summary.startswith(f"supervisor sweep: {kind} (")
                and summary.endswith(SWEEP_ACTED)):
            return kind
    return None


def revision_after(conn, run_id, ticket_id, claimed, at):
    later = conn.execute(
        "SELECT revision FROM runs WHERE ticketId = ? AND id > ?"
        " ORDER BY id LIMIT 1", (ticket_id, run_id)).fetchone()
    if later is not None and later[0] is None:
        return None
    ceiling = "" if later is None else " AND revision <= ?"
    return conn.execute(
        "SELECT revision, at FROM ticketRevisions WHERE ticketId = ?"
        " AND revision > ? AND at >= ?" + ceiling + " ORDER BY revision LIMIT 1",
        (ticket_id, claimed, at, *(later or ()))).fetchone()


def stopped_criteria(conn, run_id, kind, payload):
    if kind == CONTRADICTION:
        return [each.get("id") or f"C{each['criterion']}"
                for each in json.loads(payload or "{}").get("criteria", [])]
    stuck = criteria_stuck(conn, run_id) if kind == CRITERION_STUCK else None
    return [] if stuck is None else [f"C{n}" for n in stuck[1]]


def requeue_note(conn, run_id):
    row = conn.execute(
        "SELECT note FROM interventions WHERE runId = ? AND \"action\" = ?"
        " ORDER BY id LIMIT 1", (run_id, "requeue")).fetchone()
    return None if row is None else row[0]


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
