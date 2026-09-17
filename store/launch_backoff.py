"""Persistent, project-owned evidence for a loop that cannot claim (KO-466).

The two nullable project columns hold the deadline and a JSON reason document.
The document retains the outage's start and previous interval across supervisor
restarts; expiry permits a probe, and only a passing probe clears the outage.
"""
import json

import store


def current(conn, project):
    row = conn.execute(
        "SELECT launchBackoffUntil, launchBackoffReason FROM projects WHERE id=?",
        (project,)).fetchone()
    if row is None or row[1] is None:
        return None
    return {"until": row[0], **json.loads(row[1])}


def event(conn, project, kind, note, now, run_id=None):
    """Write startup evidence without inventing a claimed run."""
    if run_id is not None:
        store.record_event(conn, run_id, kind, note, now=now)
        return
    conn.execute(
        "INSERT INTO runEvents (runId, projectId, seq, level, kind, summary, at)"
        " VALUES (NULL, ?, 1, 'narrative', ?, ?, ?)",
        (project, kind, note, now))


def intervention(conn, project, action, note, now, run_id=None):
    """One decision per target, including targets with no previous run."""
    if run_id is not None:
        store.record_intervention(conn, run_id, action, note,
                                  source="supervisor", now=now)
        return
    conn.execute(
        'INSERT INTO interventions (projectId, source, "trigger", "action", at)'
        " VALUES (?, 'supervisor', 'manual', ?, ?)", (project, action, now))
    event(conn, project, action, note, now)


def failure(conn, project, reason, now, *, pending=False, run_id=None):
    """Record the first cause, then one intervention per exponential step.

    A loop's startup failure is pending until the supervisor observes it.
    That first observation backs off without spending another probe.
    """
    with store.transaction(conn):
        previous = current(conn, project)
        if pending and previous:
            return previous["reason"]
        since = previous["since"] if previous else now
        interval = 0 if pending else min(
            1800, max(60, 2 * previous["interval"] if previous else 60))
        if previous is None:
            event(conn, project, "route_down", reason, now, run_id)
        state = {"reason": reason, "since": since, "interval": interval}
        note = f"implementer route down; retry in {interval}s: {reason}"
        if not pending:
            intervention(conn, project, "launch_backoff", note, now, run_id)
        conn.execute(
            "UPDATE projects SET launchBackoffUntil=?, launchBackoffReason=?"
            " WHERE id=?", (now + interval * 1000, json.dumps(state), project))
    return note


def clear(conn, project):
    with store.transaction(conn):
        conn.execute(
            "UPDATE projects SET launchBackoffUntil=NULL, launchBackoffReason=NULL"
            " WHERE id=?", (project,))
