from __future__ import annotations

import json
import sys
import traceback

import store.read
from holophyte.admission import project_of
from holophyte.loop.reexec import LOOP_UNIT, SUPERVISOR_UNIT, start_loop, systemctl_user
from holophyte.loop.runs import open_store
from holophyte.redact import known_secrets, outbound
from holophyte.serve.serve_runs import no_store
from store.operator_notes import send_back

ACTIONS_PREFIX = "/actions/"
UNIT_ACTIONS = {
    "restart-supervisor": ("restart", SUPERVISOR_UNIT, "restart_supervisor"),
    "launch-loop": ("start", LOOP_UNIT, "launch_loop")}
REQUEUE_ACTION = "requeue"
RECORDS = {**{action: (row,) for action, (*_, row) in UNIT_ACTIONS.items()},
           REQUEUE_ACTION: ("requeue",), "send-back": ("operator_note",),
           "merge": ("approve",), "hold": ("hold",),
           "release-hold": ("release_hold",), "pause": ("pause",),
           "resume": ("resume",), "abort": ("abort", "abort_close")}
ACTIONS = frozenset(RECORDS)
# The store refuses an empty requeue note.
DEFAULT_REQUEUE_NOTE = "requeued from the console"
MAX_BODY = 64 * 1024


def parse_action_body(raw):
    if len(raw) > MAX_BODY:
        raise ValueError(f"body must be under {MAX_BODY} bytes")
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise ValueError("body must be JSON") from None
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    return body


def unit_action(project, action, unit_name, asked=None):
    """An intervention that cannot be recorded first does not run."""
    verb, template, intervention = UNIT_ACTIONS[action]
    unit = template + unit_name
    who, route = asked or ("the daemon", f"POST /actions/{action}")
    note = f"operator asked {who} to {verb} {unit} ({route})"
    recorded = record_action_intervention(project, intervention, note)
    if recorded is None and action == "launch-loop":
        recorded = record_on_project(project, intervention, note)
    if recorded is None:
        detail = ("the store holds no run to record the intervention"
                  " against; nothing run")
        return 200, {"action": action, "ok": False, "detail": detail,
                     "unit": unit}
    if action == "launch-loop":
        _, ok, detail = start_loop(unit_name)
    else:
        ok, detail = systemctl_user(verb, unit)
    return 200, {"action": action, "ok": ok, "detail": detail, "unit": unit,
                 "recorded": recorded}


def record_action_intervention(project, action, note):
    if not project.store_path.exists():
        return None
    conn = open_store(project)
    try:
        run_id = store.read.newest_run_id(conn)
        return None if run_id is None else store.record_intervention(
            conn, run_id, action, note, source="human", trigger="manual")
    finally:
        conn.close()


def action_store(project):
    conn = open_store(project)
    conn.execute("CREATE TEMP TABLE written (id INTEGER, action TEXT)")
    conn.execute("CREATE TEMP TRIGGER note_written AFTER INSERT ON"
                 " main.interventions BEGIN INSERT INTO written"
                 " VALUES (NEW.id, NEW.action); END")
    return conn


def written_on(conn, action):
    names = RECORDS[action]
    (row,) = conn.execute(
        "SELECT MAX(id) FROM temp.written"
        f" WHERE action IN ({','.join('?' * len(names))})", names).fetchone()
    return row


def record_on_project(project, action, note):
    if not project.store_path.exists():
        return None
    conn = open_store(project)
    try:
        key = project_of(conn, project)
        return None if key is None else store.record_project_intervention(
            conn, action, note, source="human", trigger="manual",
            project_id=key)
    finally:
        conn.close()


def tickets_named(conn, identifier):
    """`linearIdentifier` is not unique: one held twice names nobody."""
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM tickets WHERE linearIdentifier = ?",
        (identifier,)).fetchone()
    return count


def requeue_action(project, body):
    action = REQUEUE_ACTION
    identifier = body.get("ticket")
    if not isinstance(identifier, str) or not identifier.strip():
        return 400, {"error": "ticket must name a mirrored ticket (KO-n)"}
    note = body.get("note", DEFAULT_REQUEUE_NOTE)
    if not isinstance(note, str) or not note.strip():
        note = DEFAULT_REQUEUE_NOTE
    identifier = identifier.strip()
    if not project.store_path.exists():
        return 503, no_store(project)
    conn = action_store(project)
    try:
        ticket = store.read.ticket_by_identifier(conn, identifier)
        if ticket is None:
            return 200, {"action": action, "ok": False, "ticket": identifier,
                         "detail": f"{identifier}: no such ticket in the store"}
        named = tickets_named(conn, identifier)
        if named > 1:
            return 200, {"action": action, "ok": False, "ticket": identifier,
                         "detail": f"{identifier} names {named} tickets in the"
                                   " store; refusing to pick one"}
        attempt = store.read.ticket_by_id(conn, ticket.id)
        latest = attempt.activeRunId or attempt.lastRunId
        requested = body.get("run", latest)
        if requested != latest:
            return 200, {"action": action, "ok": False, "ticket": identifier,
                         "detail": f"run {requested} is not {identifier}'s latest"
                                   f" attempt; run {latest} is"}
        try:
            run_id = store.requeue(conn, ticket.id, note)
        except (store.RequeueRefused, ValueError) as refused:
            return 200, {"action": action, "ok": False, "ticket": identifier,
                         "detail": str(refused)}
        recorded = written_on(conn, action)
    finally:
        conn.close()
    return 200, {"action": action, "ok": True, "ticket": identifier,
                 "detail": f"{identifier} requeued after run {run_id}",
                 "run": run_id, "recorded": recorded}


def send_back_action(project, run_id, note, author):
    if type(run_id) is not int or not 0 < run_id < 2**63:
        return 400, {"error": "run must be a positive integer"}
    if not project.store_path.exists():
        return 503, no_store(project)
    conn = action_store(project)
    try:
        event_id = send_back(conn, run_id, note, author)
        recorded = written_on(conn, "send-back")
    except (store.ApproveRefused, ValueError) as refused:
        return 200, {"ok": False, "detail": str(refused)}
    finally:
        conn.close()
    return 200, {"ok": True, "run": run_id, "event_id": event_id,
                 "recorded": recorded,
                 "detail": f"Sent back with operator_note event {event_id}"}


def action_failure(project, action, failure):
    """`SystemExit` counts: the store's `SchemaNewer` is one."""
    try:
        secrets = known_secrets(project.config())
    except (Exception, SystemExit):
        secrets = known_secrets(None)
    print(outbound(f"[holo2] action {action} failed:\n"
                   + traceback.format_exc(), secrets),
          file=sys.stderr, end="")
    return 500, {"error": outbound(
        f"{type(failure).__name__}: {failure}", secrets)}
