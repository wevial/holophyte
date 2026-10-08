from __future__ import annotations

import json
import os
import socket
from time import time

import store.read
from holophyte.cli.report import host_label, toil_status
from holophyte.config.agent_settings import budget_scale
from holophyte.config.config_tables import board_mode, sweep_config
from holophyte.config.serve_settings import serve_config
from holophyte.host.supervisor import SWEEPABLE_PHASES
from holophyte.pr.pr_status import PR_URL_RE
from holophyte.serve.serve_levers import paused_item
from holophyte.serve.serve_runs import json_host, no_store
from store.working import agent_work, chain_work, effective_work, verify_work

# A failure that strands its ticket in flight stays past it: only the
# operator will move that ticket.
FAILED_WINDOW_MS = 24 * 60 * 60 * 1000


def status(project, now=None, started_ms=None, beat_stale_ms=None):
    now = int(time() * 1000) if now is None else now
    started_ms = now if started_ms is None else started_ms
    if not project.store_path.exists():
        return 503, no_store(project)
    conn = store.read.open_readonly(project.store_path)
    try:
        runs = store.read.live_runs(conn, SWEEPABLE_PHASES)
        chains = store.read.run_chains(conn, [run.id for run in runs])
        from holophyte.loop.stop import pending_requests
        stops = pending_requests(conn)
        strikes = {run.id: store.read.strike(conn, run.id) for run in runs}
        beat = store.read.supervisor_beat(conn)
        from holophyte.admission import state
        admission, hold_note = state(conn, project)
        if admission == "disabled":
            runs = []
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
        toil = toil_status(conn, now)
    finally:
        conn.close()
    knobs = sweep_config(project)
    from holophyte.agents.agent_turns import route_labels
    from holophyte.serve.serve_runs import active_routes, workers_on_previous_build

    # The box the loop armed, judged against `agent_ms`, not `working_ms`.
    scale = budget_scale(project)
    return 200, {
        "project": str(project.path),
        "admission": admission, "hold_note": hold_note,
        "schema_version": schema_version,
        "active_routes": active_routes(project),
        "route_labels": route_labels(project),
        "workers_on_previous_build": workers_on_previous_build(project),
        "host": host_label(project, socket.gethostname()),
        "now": now, "toil": toil,
        "daemon": {"started_ms": started_ms, "pid": os.getpid()},
        "supervisor": supervisor_view(project, beat, now, knobs,
                                      beat_stale_ms),
        "thresholds": {"heartbeat_stale_ms": knobs.heartbeat_stale_ms,
                       "strikes": knobs.stale_strikes, "run_cap": knobs.run_cap},
        "actions": serve_config(project).actions,
        "config_edit": serve_config(project).config_edit,
        "runs": [{"id": run.id, "ticket": run.linearIdentifier,
                  "ticket_url": run.ticketUrl,
                  "title": run.title,
                  "phase": run.phase,
                  "stop_requested": stops.get(run.id, (None, None))[1],
                  "stop_action": stops.get(run.id, (None, None))[0],
                  "started_ms": chains[run.id][0].startedAt,
                  "heartbeat_age_ms": now - run.lastHeartbeat,
                  "elapsed_ms": now - chains[run.id][0].startedAt,
                  "working_ms": chain_work(effective_work, chains[run.id], now),
                  "work_started_ms": run.workStartedAt,
                  "agent_ms": chain_work(agent_work, chains[run.id], now),
                  "verify_ms": chain_work(verify_work, chains[run.id], now),
                  "run_count": len(chains[run.id]),
                  "verify_started_ms": run.verifyStartedAt,
                  "time_box_ms": (int(run.timeBoxMs * scale)
                                  if run.timeBoxMs else run.timeBoxMs),
                  "round": run.reviewRoundCount,
                  "strikes": (strikes[run.id].strikes
                              if strikes[run.id] is not None else 0),
                  "host": json_host(project, run.host)}
                 for run in runs],
    }


def supervisor_view(project, beat, now, knobs, stale_ms=None):
    if beat is None:
        return {"state": "none", "pid": None, "heartbeat_age_ms": None,
                "host": None}
    age = now - beat.lastBeat
    stale_ms = knobs.heartbeat_stale_ms if stale_ms is None else stale_ms
    return {"state": "live" if age < stale_ms else "stale",
            "pid": beat.pid, "heartbeat_age_ms": age,
            "host": host_label(project, beat.host)}


def parked_item(ticket):
    if ticket.outcome == "paused":
        return paused_item(ticket)
    question = ticket.blockedQuestion or ""
    if ticket.prUrl and ticket.parkKind in ("pull_request", "ci"):
        _, separator, reason = question.partition("\n")
        reason = reason if separator else question
        match = PR_URL_RE.match(ticket.prUrl)
        return {"kind": "pr_open", "ticket": ticket.linearIdentifier,
                "ticket_url": ticket.ticketUrl, "title": ticket.title,
                "run": ticket.runId, "pr_url": ticket.prUrl,
                "reason": reason, "asked_ms": ticket.askedMs,
                "pr": {"number": int(match.group(4)) if match else None,
                       "checks": ticket.prSeenChecks,
                       "review": ticket.prSeenReview,
                       "threads": ticket.prSeenThreads,
                       "title": ticket.prSeenTitle},
                "level": "attention"}
    return {"kind": "blocked", "ticket": ticket.linearIdentifier,
            "ticket_url": ticket.ticketUrl,
            "question": ticket.blockedQuestion,
            "run": ticket.runId, "asked_ms": ticket.askedMs,
            "pr_url": ticket.prUrl, "level": "attention"}


def triage_view(triage):
    if triage is None:
        return None
    return {key: triage.get(key) for key in
            ("choice", "confidence", "backend", "model", "requeued")}


def attention(project, now=None, beat_stale_ms=None):
    """Never `critical`, a client's level for a daemon it cannot reach."""
    now = int(time() * 1000) if now is None else now
    if not project.store_path.exists():
        return 503, no_store(project)
    conn = store.read.open_readonly(project.store_path)
    try:
        blocked = store.read.blocked_tickets(conn)
        runs = store.read.live_runs(conn, SWEEPABLE_PHASES)
        failed = store.read.recent_failed_runs(conn, now - FAILED_WINDOW_MS)
        stranded = store.read.stranded_runs(conn)
        beat = store.read.supervisor_beat(conn)
    finally:
        conn.close()
    windowed = {run.id for run in failed}
    failed = sorted(failed + [run for run in stranded if run.id not in windowed],
                    key=lambda run: (run.endedAt, run.id))
    knobs = sweep_config(project)
    items = [parked_item(ticket) for ticket in blocked
             if ticket.boardState not in ("Backlog", "Canceled", "Done")]
    for run in runs:
        age = now - run.lastHeartbeat
        if (age > knobs.heartbeat_stale_ms
                and run.boardState not in ("Backlog", "Canceled", "Done")):
            items.append({"kind": "stale_run", "run": run.id,
                          "ticket": run.linearIdentifier,
                          "ticket_url": run.ticketUrl, "phase": run.phase,
                          "heartbeat_age_ms": age, "pr_url": run.prUrl,
                          "level": "attention"})
    items.extend({"kind": "failed", "run": run.id,
                  "ticket": run.linearIdentifier,
                  "ticket_url": run.ticketUrl, "reason": run.outcomeReason,
                  "ended_ms": run.endedAt, "attempt": run.attempt,
                  "pr_url": run.prUrl, "triage": triage_view(run.triage),
                  "level": "attention"}
                 for run in failed if run.id == run.lastRunId
                 and run.activeRunId is None
                 and run.ticketStatus in ("ready", "in_flight", "blocked_on_operator")
                 and run.boardState not in ("Backlog", "Canceled", "Done"))
    supervisor = supervisor_view(project, beat, now, knobs, beat_stale_ms)
    if supervisor["state"] != "live":
        items.append({"kind": "supervisor", "state": supervisor["state"],
                      "heartbeat_age_ms": supervisor["heartbeat_age_ms"],
                      "level": "attention"})
    if items:
        level = "attention"
    else:
        level = "working" if runs else "none"
    return 200, {"level": level, "items": items, "now": now,
                 "project": str(project.path)}


BOARD_STATES = ("needs_spec", "blocked_on_deps", "ready",
                "blocked_on_operator", "in_flight")
BACKLOG = "backlog"
IDLE_STATES = ("needs_spec", "blocked_on_deps", "ready")


def board(project, now=None, editable=False):
    """The store's mirror is the whole answer: the provider is never asked."""
    now = int(time() * 1000) if now is None else now
    if not project.store_path.exists():
        return 503, no_store(project)
    conn = store.read.open_readonly(project.store_path)
    try:
        tickets = store.read.open_tickets(conn)
    finally:
        conn.close()
    native = board_mode(project).kind == "native"
    states = ((BACKLOG,) if native else ()) + BOARD_STATES
    columns = {state: [] for state in states}
    for ticket in tickets:
        backlog = (native and ticket.boardColumn == BACKLOG
                   and ticket.status in IDLE_STATES)
        columns[BACKLOG if backlog else ticket.status].append({
            "ticket": ticket.linearIdentifier,
            "ticket_url": ticket.ticketUrl, "title": ticket.title,
            "time_box_ms": ticket.timeBoxMs, "run": ticket.activeRunId,
            "question": ticket.blockedQuestion,
            "waits_on": list(ticket.waitsOn),
            "mirrored_ms": ticket.mirroredAt, "column": ticket.boardColumn,
            "priority": ticket.priority, "labels": list(ticket.labels),
            "revision": ticket.revision})
    return 200, {"columns": [{"state": state, "tickets": columns[state]}
                             for state in states],
                 "editable": editable and native, "now": now}


def ticket_detail(project, identifier):
    if not project.store_path.exists():
        return 503, no_store(project)
    conn = store.read.open_readonly(project.store_path)
    try:
        ticket = store.read.ticket_by_identifier(conn, identifier)
        revisions = ([] if ticket is None
                     else store.read.ticket_revisions(conn, ticket.id))
        notes = ([] if ticket is None
                 else store.read.ticket_notes(conn, ticket.id))
    finally:
        conn.close()
    if ticket is None:
        return 404, {}
    by_number = {r.revision: r for r in revisions}
    return 200, {"ticket": ticket.linearIdentifier,
                 "ticket_url": ticket.ticketUrl, "title": ticket.title,
                 "status": ticket.status, "body": ticket.body,
                 "acceptance_criteria": list(ticket.acceptanceCriteria),
                 "verification_commands": list(ticket.verificationCommands),
                 "time_box_ms": ticket.timeBoxMs, "run": ticket.activeRunId,
                 "mirrored_ms": ticket.mirroredAt,
                 "current": revision_json(by_number.get(ticket.revision)),
                 "claimed": claimed_json(ticket, by_number),
                 "revisions": [{"revision": r.revision, "at": r.at,
                                "author": r.author} for r in revisions],
                 "notes": [{"id": n.id, "at": n.at, "author": n.author,
                            "kind": n.kind, "text": n.text,
                            "posted_ms": n.postedAt,
                            "post_error": n.postError} for n in notes]}


def revision_json(revision):
    if revision is None:
        return None
    return {"revision": revision.revision, "at": revision.at,
            "author": revision.author, "title": revision.title,
            "body": revision.body, "priority": revision.priority,
            "labels": list(revision.labels), "column": revision.column}


def claimed_json(ticket, by_number):
    """A run the previous build claimed has no `runs.revision`: its snapshot."""
    if ticket.activeRunId is None:
        return None
    if ticket.claimedRevision is not None:
        return revision_json(by_number.get(ticket.claimedRevision))
    if ticket.claimedSnapshot is None:
        return None
    snapshot = json.loads(ticket.claimedSnapshot)
    return {"revision": None, "title": snapshot["title"],
            "acceptance_criteria": snapshot["acceptanceCriteria"],
            "verification_commands": snapshot["verificationCommands"]}
