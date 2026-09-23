"""holophyte.serve_levers: the daemon's hold, release-hold, pause and resume
routes (KO-609), dispatched from `do_POST()` behind the actions' token and
`[serve] actions = true` gate.

Each lever is the CLI's own store call -- `--hold`/`--release-hold`
through `holophyte.admission.set_hold()` on the daemon's project, `--pause`
through `store.pause()`, `--resume` through `holophyte.stop.resume_paused()`
-- so the console and the shell cannot disagree about what a lever does.
Every body carries `note`, the operator's reason, and an optional `author`
(default `maintainer`, as `send-back` has); what the store records is
`"{author} via the console: {note}"`, since `interventions` has no actor
column and who pulled the lever belongs with why. A missing or blank note
is 400 and writes nothing; a missing store is 503; a refusal from the store
(already held, run already ended, not paused) is 200 with `ok: false` and
the refusal in `detail`, as `requeue` answers. `paused_item()` is the
`/attention` item for a ticket a pause parked, the one `resume` releases.
"""
from __future__ import annotations

import store
import store.read
from holophyte.admission import held_line, set_hold
from holophyte.runs import open_store
from holophyte.serve_actions import tickets_named
from holophyte.serve_runs import no_store
from holophyte.stop import resume_paused

DEFAULT_AUTHOR = "maintainer"


def recorded_reason(body):
    """`"{author} via the console: {note}"` from the body, or None when its
    `note` is missing, not text or blank."""
    note = body.get("note")
    if not isinstance(note, str) or not note.strip():
        return None
    author = body.get("author", DEFAULT_AUTHOR)
    if not isinstance(author, str) or not author.strip():
        author = DEFAULT_AUTHOR
    return f"{author.strip()} via the console: {note.strip()}"


def lever(action, target, body, act):
    """Parse the reason, open the store, run `act(conn, reason)` for its
    `(ok, detail, extra fields)`, and answer `(http status, JSON body)`;
    ValueError or `store.ResumeRefused` from `act` is the store's refusal."""
    reason = recorded_reason(body)
    if reason is None:
        return 400, {"error": "note must say why (non-blank text)"}
    if not target.store_path.exists():
        return 503, no_store(target)
    conn = open_store(target)
    try:
        ok, detail, extra = act(conn, reason)
    except (ValueError, store.ResumeRefused) as refused:
        return 200, {"action": action, "ok": False, "detail": str(refused)}
    finally:
        conn.close()
    return 200, {"action": action, "ok": ok, "detail": detail, **extra}


def admission_action(action, holding):
    """The `hold` or `release-hold` handler: `set_hold()` on the daemon's
    own project, the interventions row landing before `admission` moves."""
    def handler(target, body):
        def act(conn, reason):
            project = set_hold(conn, target, holding, reason)
            line = held_line(conn, project) or f"project {target.path} enabled"
            return True, line, {"reason": reason}
        return lever(action, target, body, act)
    return handler


def pause_action(target, body):
    """`POST /actions/pause`: `store.pause()` on the live run `run` names;
    `stopRequested` then names the `pause` intervention carrying the reason.
    A run already ended is `ok: false` naming its outcome."""
    run_id = body.get("run")
    if type(run_id) is not int or not 0 < run_id < 2**63:
        return 400, {"error": "run must be a positive integer"}

    def act(conn, reason):
        request = store.pause(conn, run_id, reason)
        return True, f"pause requested for run {run_id}", {
            "run": run_id, "recorded": request}
    return lever("pause", target, body, act)


def resume_action(target, body):
    """`POST /actions/resume`: `resume_paused()` on the ticket `ticket`
    names -- `--resume`'s own helper, so the pull request's pause notice is
    cleared as well -- walking it back to `ready`. An unknown or ambiguous
    identifier is `ok: false` and writes nothing, as `requeue` refuses."""
    identifier = body.get("ticket")
    if not isinstance(identifier, str) or not identifier.strip():
        return 400, {"error": "ticket must name a mirrored ticket (KO-n)"}
    identifier = identifier.strip()

    def act(conn, reason):
        ticket = store.read.ticket_by_identifier(conn, identifier)
        if ticket is None:
            return False, f"{identifier}: no such ticket in the store", {}
        named = tickets_named(conn, identifier)
        if named > 1:
            return False, (f"{identifier} names {named} tickets in the store;"
                           " refusing to pick one"), {}
        run_id = resume_paused(target, conn, ticket.id, reason)
        return True, f"{identifier} ready to resume run {run_id}", {
            "ticket": identifier, "run": run_id}
    return lever("resume", target, body, act)


def paused_item(ticket):
    """The `/attention` item for a `blocked_on_operator` ticket whose latest
    run ended `paused`: kind `paused`, its `note` the pause's reason, so a
    console offers resume rather than an answer to a question."""
    return {"kind": "paused", "ticket": ticket.linearIdentifier,
            "ticket_url": ticket.ticketUrl, "title": ticket.title,
            "note": ticket.blockedQuestion, "run": ticket.runId,
            "asked_ms": ticket.askedMs, "pr_url": ticket.prUrl,
            "level": "attention"}


# Route name -> handler(target, body); `ACTIONS` in `holophyte.serve_actions`
# names the same four.
LEVERS = {
    "hold": admission_action("hold", True),
    "release-hold": admission_action("release-hold", False),
    "pause": pause_action,
    "resume": resume_action,
}
