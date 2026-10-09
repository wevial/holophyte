import json

import store
from store.operate import _release_parked
from store.schema import _transaction

ASK = "console_ask"
ANSWERED = "console_ask_answered"


class AskRefused(Exception):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason


def ask(conn, run_id, question, author):
    if not isinstance(question, str) or not question.strip():
        raise AskRefused("empty_question", "the question must be non-blank text")
    data = {"question": question.strip(), "author": author}
    with _transaction(conn):
        ticket_id, pr_url, outcome = conn.execute(
            "SELECT ticketId, prUrl, outcome FROM runs WHERE id = ?",
            (run_id,)).fetchone()
        if pr_url is None:
            raise AskRefused("no_pull_request",
                             f"run {run_id} opened no pull request to answer on")
        waiting = pending(conn, run_id)
        if waiting:
            raise AskRefused("ask_pending",
                             f"console ask event {waiting[0]['id']} on {pr_url}"
                             " is not answered yet")
        if outcome not in (None, "paused"):
            raise AskRefused("finished", f"run {run_id} has ended {outcome}")

        def record_ask():
            store.record_event(conn, run_id, ASK,
                               f"{author} asked: {data['question']}",
                               level="detail", payload=json.dumps(data))
        _release_parked(conn, ticket_id, "babysit",
                        f"{author} via the console: ask: {data['question']}",
                        "sent back to the babysitter to answer a console ask",
                        None, require_pr=True, before_release=record_ask,
                        run_id=run_id)
        return conn.execute("SELECT id FROM runEvents WHERE runId = ?"
                            " AND kind = ? ORDER BY id DESC LIMIT 1",
                            (run_id, ASK)).fetchone()[0]


def asks(conn, run_id, pr_url=None):
    if conn is None or run_id is None:
        return []
    rows = conn.execute(
        "SELECT e.id, e.payload, e.at, a.payload, a.at FROM runEvents e"
        " JOIN runs r ON r.id = e.runId JOIN runs current ON current.id = ?"
        " LEFT JOIN runEvents a ON a.id = (SELECT MIN(x.id) FROM runEvents x"
        " JOIN runs xr ON xr.id = x.runId WHERE xr.ticketId = current.ticketId"
        " AND x.kind = ? AND json_extract(x.payload, '$.event_id') = e.id)"
        " WHERE e.kind = ? AND r.ticketId = current.ticketId"
        " AND r.prUrl = COALESCE(current.prUrl, ?) ORDER BY e.id",
        (run_id, ANSWERED, ASK, pr_url)).fetchall()
    result = []
    for event_id, payload, asked_ms, answer, answered_ms in rows:
        data, answer = json.loads(payload), json.loads(answer or "{}")
        result.append({"id": event_id, "question": data["question"],
                       "author": data["author"], "asked_ms": asked_ms,
                       "answered_ms": answered_ms, "url": answer.get("url"),
                       "answer": answer.get("answer")})
    return result


def pending(conn, run_id, pr_url=None):
    return [a for a in asks(conn, run_id, pr_url) if a["answered_ms"] is None]


def answered(conn, run_id, event_id, url, answer):
    store.record_event(conn, run_id, ANSWERED,
                       f"console ask event {event_id} answered"
                       f" on {url or 'the pull request'}",
                       level="detail", payload=json.dumps(
                           {"event_id": event_id, "url": url, "answer": answer}))
