"""Record an explicitly configured route substitution (KO-467)."""
import json
from time import time

import store
from store import launch_backoff


def switched(conn, project, evidence, run_id=None):
    """One project intervention and an event, atomically before activation."""
    now = int(time() * 1000)
    note = json.dumps(evidence)
    with store.transaction(conn):
        conn.execute(
            'INSERT INTO interventions (projectId, runId, source, "trigger",'
            ' "action", guidance, at)'
            " VALUES (?, ?, 'supervisor', 'manual', 'route_fallback', ?, ?)",
            (project, run_id, note, now))
        if run_id is None:
            launch_backoff.event(conn, project, 'route_fallback', note, now)
        else:
            store.record_event(conn, run_id, 'route_fallback', note,
                               now=now)
