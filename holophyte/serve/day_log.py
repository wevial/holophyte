"""`GET /ledger`'s park rows and the failed runs with no `failure` row."""
from __future__ import annotations

THREAD_PARK = "a thread needs a human's answer"


def park_rows(conn, since, limit):
    rows = conn.execute(
        "SELECT e.at, e.runId, t.linearIdentifier, r.prUrl, e.summary"
        " FROM runEvents e JOIN runs r ON r.id = e.runId"
        " JOIN tickets t ON t.id = r.ticketId"
        " WHERE e.kind = 'phase_change' AND e.at >= ?"
        " AND (e.summary LIKE '% -> blocked_on_operator'"
        "  OR e.summary LIKE '% -> blocked_on_operator: %'"
        "  OR (e.summary LIKE '% -> awaiting_merge_approval: %'"
        "   AND instr(e.summary, ?) > 0))"
        " ORDER BY e.at DESC, e.id DESC LIMIT ?",
        (since, THREAD_PARK, limit)).fetchall()
    return [{"at": at, "run": run, "ticket": ticket, "kind": "park",
             "source": "factory", "text": summary.partition(": ")[2],
             "pr_url": pr_url}
            for at, run, ticket, pr_url, summary in rows]


def failure_rows(conn, since, limit):
    rows = conn.execute(
        "SELECT r.endedAt, r.id, t.linearIdentifier, r.prUrl, r.failureKind,"
        " r.outcomeReason FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.outcome = 'failed' AND r.endedAt >= ?"
        " AND NOT EXISTS (SELECT 1 FROM ledger l"
        "  WHERE l.runId = r.id AND l.kind = 'failure')"
        " ORDER BY r.endedAt DESC, r.id DESC LIMIT ?",
        (since, limit)).fetchall()
    return [{"at": at, "run": run, "ticket": ticket, "kind": "failure",
             "source": "factory", "text": reason or "", "pr_url": pr_url,
             "failure_kind": failure_kind or "unclassified"}
            for at, run, ticket, pr_url, failure_kind, reason in rows]
