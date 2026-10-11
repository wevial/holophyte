import json

import store
import ticket_template
from holophyte import questions, redact
from holophyte.loop.failure_triage import asked_route
from holophyte.loop.stop import continuation
from holophyte.redact import safe_print as print

EVENT = "complexity"
BODY_CHARS = 24000
REASON_CHARS = 200


def classifier_profile(run, task):
    """The profile a confident verdict maps to under `route = true`, else None."""
    try:
        options = questions.settings(run.project.config())["complexity"]
        if options is None or run.conn is None or run.run_id is None:
            return None
        verdict = recorded(run.conn, run.run_id) or classify(run, task, options)
    except Exception as error:
        print(f"[holo2] complexity question skipped: {error}")
        return None
    if (not options["route"] or verdict["verdict"] is None
            or verdict["confidence"] < options["route_confidence"]):
        return None
    return options["profiles"].get(verdict["verdict"])


def recorded(conn, run_id):
    row = conn.execute(
        "SELECT e.runId, e.payload FROM runEvents e JOIN runs r ON r.id = e.runId"
        " JOIN runs current ON current.ticketId = r.ticketId"
        " WHERE current.id = ? AND r.id <= current.id AND e.kind = ?"
        " ORDER BY e.id DESC LIMIT 1", (run_id, EVENT)).fetchone()
    if row is None or (row[0] != run_id and continuation(conn, run_id) is None):
        return None
    return json.loads(row[1])


def classify(run, task, options):
    config, conn, run_id = run.project.config(), run.conn, run.run_id
    answer = questions.ask(questions.COMPLEXITY, ticket_state(task, config),
                           config=config, conn=conn, run_id=run_id,
                           seat="complexity")
    backend, model, _ = asked_route(conn, run_id, options, questions.COMPLEXITY)
    answered = isinstance(answer, questions.Answer)
    reason = None
    if answered and answer.reason is not None:
        lines = redact.outbound(answer.reason, redact.known_secrets(config))
        reason = (lines.splitlines() or [""])[0][:REASON_CHARS]
    payload = dict(verdict=answer.choice if answered else None,
                   confidence=answer.confidence if answered else None,
                   reason=reason, backend=backend, model=model,
                   why=None if answered else answer.reason)
    summary = (f"complexity: {answer.choice} ({answer.confidence:.2f})"
               if answered else f"complexity unanswered: {answer.reason}")
    store.record_event(conn, run_id, EVENT, summary, level="detail",
                       payload=json.dumps(payload))
    print(f"[holo2] {summary}")
    return payload


def ticket_state(task, config):
    body = task.get("body") or ""
    ticket = ticket_template.parse(body)
    secrets = redact.known_secrets(config)
    return dict(title=redact.outbound(task.get("title") or "", secrets),
                body=redact.outbound(body, secrets)[:BODY_CHARS],
                estimate_min=ticket.estimate_min,
                criteria=len(ticket.acceptance) + len(ticket.acceptance_done)
                + len(ticket.acceptance_other))
