from __future__ import annotations

import store
import store.read
from holophyte.admission import held_line, set_hold
from holophyte.loop.runs import open_store
from holophyte.loop.stop import abort_run, resume_paused
from holophyte.serve.serve_actions import tickets_named
from holophyte.serve.serve_runs import no_store

DEFAULT_AUTHOR = "maintainer"


def recorded_reason(body):
    """`interventions` has no actor column, so the author rides in the note."""
    note = body.get("note")
    if not isinstance(note, str) or not note.strip():
        return None
    return f"{console_author(body)} via the console: {note.strip()}"


def console_author(body):
    author = body.get("author", DEFAULT_AUTHOR)
    if not isinstance(author, str) or not author.strip():
        return DEFAULT_AUTHOR
    return author.strip()


def lever(action, target, body, act):
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
    def handler(target, body):
        def act(conn, reason):
            project = set_hold(conn, target, holding, reason)
            line = held_line(conn, project) or f"project {target.path} enabled"
            return True, line, {"reason": reason}
        return lever(action, target, body, act)
    return handler


def pause_action(target, body):
    run_id = body.get("run")
    if type(run_id) is not int or not 0 < run_id < 2**63:
        return 400, {"error": "run must be a positive integer"}

    def act(conn, reason):
        request = store.pause(conn, run_id, reason)
        return True, f"pause requested for run {run_id}", {
            "run": run_id, "recorded": request}
    return lever("pause", target, body, act)


def resume_action(target, body):
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


def abort_action(target, body):
    run_id = body.get("run")
    if type(run_id) is not int or not 0 < run_id < 2**63:
        return 400, {"error": "run must be a positive integer"}
    close = body.get("close", False)
    if type(close) is not bool:
        return 400, {"error": "close must be true or false"}

    def act(conn, reason):
        from provider import board_for
        board = board_for(target)
        if board is None:
            return False, (f"{target.config_path}: [board] project_id is not"
                           " set; nothing written"), {"run": run_id}
        ended = abort_run(target, conn, run_id, reason,
                          provider=board, close=close)
        detail = (f"run {run_id} had no live worker; ended abandoned and parked"
                  if ended else f"abort requested; run {run_id} ends at its"
                  " worker's next heartbeat")
        return True, detail, {"run": run_id, "close": close, "ended": ended}
    return lever("abort", target, body, act)


def paused_item(ticket):
    return {"kind": "paused", "ticket": ticket.linearIdentifier,
            "ticket_url": ticket.ticketUrl, "title": ticket.title,
            "note": ticket.blockedQuestion, "run": ticket.runId,
            "asked_ms": ticket.askedMs, "pr_url": ticket.prUrl,
            "level": "attention"}


# `ACTIONS` in `holophyte.serve.serve_actions` names the same five.
LEVERS = {
    "hold": admission_action("hold", True),
    "release-hold": admission_action("release-hold", False),
    "pause": pause_action,
    "resume": resume_action,
    "abort": abort_action,
}
