import json

import store
from holophyte import questions, redact
from holophyte.redact import safe_print as print

FAILURE_CAUSE = questions.Question(
    "A software factory run failed while implementing a ticket. Decide what "
    "caused the failure from the record: the failure reason, its kind and "
    "class, the attempt number and the run's last review.",
    {
        "infra": "A tool, network, vendor, CI-host or factory-configuration "
                 "problem outside the change: a transient API or command error, "
                 "an unreachable or crashed tool, or a verify command that timed "
                 "out under the project's configured limit after the review "
                 "approved the change",
        "code": "The change is wrong: its tests or verify commands fail on their "
                "merits, or the review found defects the change did not fix",
        "spec": "The ticket is wrong: its criteria contradict each other or the "
                "code, or its verify commands cannot pass as written",
    },
    "failure_cause",
)
REASON_CHARS = 2000
FINDINGS_CHARS = 700
NOTE_PREFIX = "failure triage:"


def triage_failure(target, conn, run_id, ticket_id):
    try:
        options = questions.settings(target.config())["failures"]
        if options is not None and not swept(conn, run_id):
            decide(target.config(), options, conn, run_id, ticket_id)
    except Exception as error:
        print(f"[holo2] failure triage skipped: {error}")


def swept(conn, run_id):
    return conn.execute("SELECT failureKind FROM runs WHERE id = ?",
                        (run_id,)).fetchone() == ("swept",)


def decide(config, options, conn, run_id, ticket_id):
    answer = questions.ask(FAILURE_CAUSE, failure_state(conn, run_id, config),
                           config=config, conn=conn, run_id=run_id,
                           seat="failures")
    route = asked_route(conn, run_id, options)
    why = held_back(answer, options, conn, ticket_id)
    if why is None:
        note = (f"{NOTE_PREFIX} {answer.choice} ({answer.confidence:.2f},"
                f" {' '.join(filter(None, route))}); requeued once on the preserved"
                " branch; a further failure waits for the operator")
        try:
            store.requeue(conn, ticket_id, note, source="factory")
            why = "requeued"
        except store.RequeueRefused as refused:
            why = f"requeue refused: {refused}"
    answered = isinstance(answer, questions.Answer)
    payload = dict(choice=answer.choice if answered else None,
                   confidence=answer.confidence if answered else None,
                   backend=route[0], model=route[1], requeued=why == "requeued",
                   why=why)
    summary = (f"{NOTE_PREFIX} {answer.choice} ({answer.confidence}): {why}"
               if answered else f"{NOTE_PREFIX} unanswered: {why}")
    store.record_event(conn, run_id, "failure_triage", summary, level="detail",
                       payload=json.dumps(payload))
    print(f"[holo2] {summary}")


def failure_state(conn, run_id, config):
    row = conn.execute(
        "SELECT t.title, r.outcomeReason, r.failureKind, r.outcomeClass,"
        " r.attempt, rr.verdict, rr.findings"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " LEFT JOIN reviewRounds rr ON rr.id = (SELECT id FROM reviewRounds"
        "  WHERE runId = r.id ORDER BY round DESC LIMIT 1)"
        " WHERE r.id = ?", (run_id,)).fetchone()
    secrets = redact.known_secrets(config)

    def outbound(text, limit=None):
        return None if text is None else redact.outbound(text, secrets)[:limit]

    title, reason, kind, outcome_class, attempt, verdict, findings = row
    return dict(ticket_title=outbound(title),
                reason=outbound(reason, REASON_CHARS),
                failure_kind=outbound(kind),
                outcome_class=outbound(outcome_class),
                attempt=attempt,
                last_review_verdict=outbound(verdict),
                last_review_findings=outbound(findings, FINDINGS_CHARS))


def asked_route(conn, run_id, options):
    row = conn.execute(
        "SELECT payload FROM runEvents WHERE runId = ? AND kind = 'question'"
        " ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
    asked = json.loads(row[0]) if row else {}
    if asked.get("question") != FAILURE_CAUSE.name:
        asked = options
    return asked["backend"], asked["model"], asked["effort"]


def held_back(answer, options, conn, ticket_id):
    if isinstance(answer, questions.Failure):
        return answer.reason
    if not options["requeue"]:
        return "requeue off"
    if answer.choice != "infra":
        return "not infra"
    if answer.confidence < options["requeue_confidence"]:
        return "below requeue_confidence"
    if conn.execute(
            "SELECT 1 FROM interventions i JOIN runs r ON r.id = i.runId"
            " WHERE r.ticketId = ? AND i.source = 'factory'"
            " AND i.\"action\" = 'requeue' AND substr(i.note, 1, ?) = ?",
            (ticket_id, len(NOTE_PREFIX), NOTE_PREFIX)).fetchone():
        return "already auto-requeued"
    status, active_run_id = conn.execute(
        "SELECT status, activeRunId FROM tickets WHERE id = ?",
        (ticket_id,)).fetchone()
    if status != "in_flight" or active_run_id is not None:
        return "ticket not in flight"
    return None
