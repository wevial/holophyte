"""holophyte.serve_actions: the daemon's `POST /actions/...` routes (KO-395).

Moved verbatim out of `holophyte/serve.py`: the `POST` body's parser
`parse_action_body()`, the unit actions' `unit_action()`, the `requeue`
action's `requeue_action()` with `tickets_named()` -- the
duplicate-identifier check `--requeue` makes -- and
`record_action_intervention()`, the interventions row an action lands
before it acts, shared with `PUT /config`'s write. The constants the
region owns came with it -- `ACTIONS_PREFIX`, `UNIT_ACTIONS`,
`REQUEUE_ACTION`, `ACTIONS`, `DEFAULT_REQUEUE_NOTE` and `MAX_BODY`.
`no_store()`, which `requeue_action()` shares with the read routes,
lives with them in `holophyte.serve_runs`, so the import runs one way;
`holophyte.serve_config`'s `_write_config()` reaches
`record_action_intervention()` here through a deferred
`from holophyte.serve_actions import`.
"""
from __future__ import annotations

import json

import store.read
from holophyte.reexec import LOOP_UNIT, SUPERVISOR_UNIT, start_loop, systemctl_user
from holophyte.runs import open_store
from holophyte.serve_runs import no_store

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
ACTIONS = frozenset(UNIT_ACTIONS) | {REQUEUE_ACTION}
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
    body)`.

    The store's one transaction is the whole write -- the `requeue`
    interventions row carrying the note and the ticket walked to `ready`
    -- exactly what `--requeue KO-n --note TEXT` does. A missing or
    non-string `ticket` is 400; a store the target does not have is 503;
    a ticket the store never mirrored, one it holds more than once (the
    CLI refuses to pick one; so does the route), or one the store refuses
    to requeue (a live run, not `in_flight`, its last run not `failed`),
    is 200 with `ok: false` and the refusal in `detail`, nothing written.
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
