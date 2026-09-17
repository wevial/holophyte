"""One persisted work interval per run, independent of its wall-clock lifetime."""
from contextlib import contextmanager
from contextvars import ContextVar
from time import time

import store
from store.schema import transaction

_active = ContextVar('working_runs', default=frozenset())


def effective_work(run, now=None):
    """Read committed plus in-flight milliseconds; None means unmeasured."""
    if run.workingMs is None:
        return None
    now = int(time() * 1000) if now is None else now
    return run.workingMs + (now - run.workStartedAt
                            if run.workStartedAt is not None else 0)


def settle_work(conn, run_id, now=None):
    """Commit and clear the interval atomically, joining a sweep transaction.

    NULL totals stay unmeasured. A second settlement (including a late work
    return after a sweep) writes nothing and cannot change the run's outcome.
    """
    now = int(time() * 1000) if now is None else now
    with transaction(conn):
        conn.execute(
            'UPDATE runs SET workingMs = workingMs + (? - workStartedAt),'
            ' workStartedAt = NULL WHERE id = ? AND workStartedAt IS NOT NULL',
            (now, run_id))


def _start_work(conn, run_id):
    with transaction(conn):
        row = conn.execute(
            'SELECT endedAt, outcome, outcomeReason FROM runs WHERE id = ?',
            (run_id,)).fetchone()
        if row is None:
            raise ValueError(f'no run {run_id}')
        if row[0] is not None:
            raise store.RunEnded(run_id, row[1], row[2])
        changed = conn.execute(
            'UPDATE runs SET workStartedAt = ?'
            ' WHERE id = ? AND workStartedAt IS NULL AND endedAt IS NULL'
            ' AND EXISTS (SELECT 1 FROM tickets WHERE tickets.id = runs.ticketId'
            ' AND tickets.activeRunId = runs.id)',
            (int(time() * 1000), run_id)).rowcount
        if not changed:
            raise store.ClaimConflict(f'run {run_id} does not own an idle work clock')


@contextmanager
def working(conn, run_id):
    """Finally-safe role/verify boundary; nested wrappers share the outer span.

    Only this context's outer owner settles. Other callers cannot take over
    an already running interval; starting work also requires the ticket lease.
    No transaction is held across the work itself.
    """
    key = (conn, run_id)
    if conn is None or run_id is None or key in _active.get():
        yield
        return
    _start_work(conn, run_id)
    token = _active.set(_active.get() | {key})
    try:
        yield
    finally:
        try:
            settle_work(conn, run_id)
        finally:
            _active.reset(token)
