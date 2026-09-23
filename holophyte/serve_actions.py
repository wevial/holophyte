"""holophyte.serve_actions: the daemon's `POST /actions/...` routes (KO-395).

Owns POST parsing, unit actions, private maintainer send-back notes,
and requeue with the CLI's duplicate-ticket
check. `record_action_intervention()` records before acting and is shared
with `PUT /config`. This module also owns the route constants:
`ACTIONS_PREFIX`, `UNIT_ACTIONS`,
`REQUEUE_ACTION`, `ACTIONS`, `DEFAULT_REQUEUE_NOTE` and `MAX_BODY`.
`no_store()`, which `requeue_action()` shares with the read routes,
lives with them in `holophyte.serve_runs`, so the import runs one way;
`holophyte.serve_config`'s `_write_config()` reaches
`record_action_intervention()` here through a deferred
`from holophyte.serve_actions import`.
"""
from __future__ import annotations

import json
import sys
import traceback

import store.read
from holophyte.config import serve_config
from holophyte.redact import known_secrets, outbound
from holophyte.reexec import LOOP_UNIT, SUPERVISOR_UNIT, start_loop, systemctl_user
from holophyte.runs import open_store
from holophyte.serve_runs import no_store
from store.operator_notes import send_back

# The token-gated `POST` routes `[serve] actions = true` opens (KO-348),
# each the operator-ladder step it maps to. The two unit actions name the
# deploy templates with the `[serve] name` instance appended at request
# time, and `holophyte.reexec` runs `systemctl` -- `launch-loop` through
# the `start_loop()` the supervisor's sweep also calls (KO-376).
ACTIONS_PREFIX = "/actions/"
# Route name -> (systemctl verb, unit template, interventions action).
UNIT_ACTIONS = {
    "restart-supervisor": ("restart", SUPERVISOR_UNIT, "restart_supervisor"),
    "launch-loop": ("start", LOOP_UNIT, "launch_loop")}
REQUEUE_ACTION = "requeue"
# The five levers `holophyte.serve_levers.LEVERS` answers (KO-609, KO-612).
ACTIONS = frozenset(UNIT_ACTIONS) | {REQUEUE_ACTION, "send-back", "hold",
                                     "release-hold", "pause", "resume",
                                     "abort"}
# The note a requeue records when the request carries none: the store
# refuses an empty one, and the CLI's `--note` is the operator's reason.
DEFAULT_REQUEUE_NOTE = "requeued from the console"
# How much JSON a `POST` body may carry; a ticket and a note are far under.
MAX_BODY = 64 * 1024


def parse_action_body(raw):
    """The `POST /actions/...` body as a dict; ValueError when it is not
    JSON, not an object, or past `MAX_BODY`. An empty body is `{}`."""
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


def unit_action(target, action, unit_name):
    """Run the `systemctl --user` step `action` names against the unit
    instance `unit_name`: `(http status, JSON-able body)`.

    The interventions row lands first (`store.record_intervention()`, the
    operator ladder's record-before-acting call), on the store's newest run
    (`store.read.newest_run_id()`) since interventions are keyed by run. A
    target with no store, or a store with no run yet, has nothing to record
    against and the step does not run: 200 with `ok: false` saying so,
    since an unrecorded hand on the units is what the ladder forbids.
    `systemctl` exiting non-zero, being absent or outliving
    `holophyte.reexec.SYSTEMCTL_TIMEOUT` is 200 with `ok: false` and the
    reason in `detail`: the operator asked for a thing and is told what
    happened, which is not a server error. `launch-loop` starts the unit
    through `start_loop()`, the call the supervisor's sweep makes when a
    ticket is ready and no loop is live (KO-376, widened by KO-409).
    """
    verb, template, intervention = UNIT_ACTIONS[action]
    unit = template + unit_name
    note = f"operator asked the daemon to {verb} {unit} (POST /actions/{action})"
    recorded = record_action_intervention(target, intervention, note)
    if recorded is None:
        detail = ("the store holds no run to record the intervention"
                  " against; nothing run")
        return 200, {"action": action, "ok": False, "detail": detail,
                     "unit": unit, "recorded": None}
    if action == "launch-loop":
        _, ok, detail = start_loop(unit_name)
    else:
        ok, detail = systemctl_user(verb, unit)
    return 200, {"action": action, "ok": ok, "detail": detail,
                 "unit": unit, "recorded": recorded}


def record_action_intervention(target, action, note):
    """Record the human `action` with `note` as an interventions row on the
    store's newest run, before the step it describes; the run's id, or None
    when the store does not exist or holds no run to record against."""
    if not target.store_path.exists():
        return None
    conn = open_store(target)
    try:
        run_id = store.read.newest_run_id(conn)
        if run_id is not None:
            store.record_intervention(conn, run_id, action, note,
                                      source="human", trigger="manual")
    finally:
        conn.close()
    return run_id


def tickets_named(conn, identifier):
    """How many mirrored tickets carry the Linear identifier `identifier`.

    `linearIdentifier` is not unique in the store -- `linearIssueId` is --
    so the same check `--requeue` makes before it writes: an identifier
    the store holds more than once names nobody, and nothing is written.
    """
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM tickets WHERE linearIdentifier = ?",
        (identifier,)).fetchone()
    return count


def requeue_action(target, body):
    """`POST /actions/requeue`: `store.requeue()` on the ticket `body`
    names, with `note` or `DEFAULT_REQUEUE_NOTE`: `(http status, JSON-able
    body)`. An optional `run` must name the ticket's latest attempt.

    The store atomically records the intervention and walks the ticket to
    `ready`. Missing/non-string tickets are 400; a missing store is 503.
    Unknown/ambiguous tickets and refused requeues return 200 with
    `ok: false` and the reason in `detail`, without writing.
    """
    action = REQUEUE_ACTION
    identifier = body.get("ticket")
    if not isinstance(identifier, str) or not identifier.strip():
        return 400, {"error": "ticket must name a mirrored ticket (KO-n)"}
    note = body.get("note", DEFAULT_REQUEUE_NOTE)
    if not isinstance(note, str) or not note.strip():
        note = DEFAULT_REQUEUE_NOTE
    identifier = identifier.strip()
    if not target.store_path.exists():
        return 503, no_store(target)
    conn = open_store(target)
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
    finally:
        conn.close()
    return 200, {"action": action, "ok": True, "ticket": identifier,
                 "detail": f"{identifier} requeued after run {run_id}",
                 "run": run_id}


def send_back_action(target, run_id, note, author):
    """Release a parked PR with a private maintainer instruction."""
    if not serve_config(target).actions:
        return 404, {"error": "actions are disabled"}
    if type(run_id) is not int or not 0 < run_id < 2**63:
        return 400, {"error": "run must be a positive integer"}
    if not target.store_path.exists():
        return 503, no_store(target)
    conn = open_store(target)
    try:
        event_id = send_back(conn, run_id, note, author)
    except (store.ApproveRefused, ValueError) as refused:
        return 200, {"ok": False, "detail": str(refused)}
    finally:
        conn.close()
    return 200, {"ok": True, "run": run_id, "event_id": event_id,
                 "detail": f"Sent back with operator_note event {event_id}"}


def action_failure(target, action, failure):
    """The 500 for an action handler that raised `failure` (KO-649), its
    traceback logged once. `SystemExit` counts: the store's `SchemaNewer`
    is one, and unanswered it closed the connection, which the console
    could only call "Failed to fetch". The `error` names the exception's
    type and message; it and the log are redacted as other outbound text
    is, with registered values alone when the config is what failed."""
    try:
        secrets = known_secrets(target.config())
    except (Exception, SystemExit):
        secrets = known_secrets(None)
    print(outbound(f"[holo2] action {action} failed:\n"
                   + traceback.format_exc(), secrets),
          file=sys.stderr, end="")
    return 500, {"error": outbound(
        f"{type(failure).__name__}: {failure}", secrets)}
