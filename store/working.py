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


def _open_verify(run, now):
    return now - run.verifyStartedAt if run.verifyStartedAt is not None else 0


def agent_work(run, now=None):
    """Read effective work less its verify part, the time the box judges.

    An open verify span is not counted; an open agent span is. A run claimed
    before `verifyMs` existed has no split, so all its work counts."""
    now = int(time() * 1000) if now is None else now
    spent = effective_work(run, now)
    if spent is None or run.verifyMs is None:
        return spent
    return spent - run.verifyMs - _open_verify(run, now)


def verify_work(run, now=None):
    """Read committed plus in-flight verify milliseconds; None means unmeasured."""
    if run.verifyMs is None:
        return None
    now = int(time() * 1000) if now is None else now
    return run.verifyMs + _open_verify(run, now)


def settle_work(conn, run_id, now=None):
    """Commit and clear the interval atomically, joining a sweep transaction.

    A verify span also adds to verifyMs. NULL totals stay unmeasured. A second
    settlement (including a late work return after a sweep) writes nothing
    and cannot change the run's outcome.
    """
    now = int(time() * 1000) if now is None else now
    with transaction(conn):
        conn.execute(
            'UPDATE runs SET workingMs = workingMs + (? - workStartedAt),'
            ' verifyMs = CASE WHEN verifyStartedAt IS NULL THEN verifyMs'
            ' ELSE verifyMs + (? - verifyStartedAt) END,'
            ' workStartedAt = NULL, verifyStartedAt = NULL'
            ' WHERE id = ? AND workStartedAt IS NOT NULL',
            (now, now, run_id))


def _start_work(conn, run_id, verify):
    with transaction(conn):
        row = conn.execute(
            'SELECT endedAt, outcome, outcomeReason FROM runs WHERE id = ?',
            (run_id,)).fetchone()
        if row is None:
            raise ValueError(f'no run {run_id}')
        if row[0] is not None:
            raise store.RunEnded(run_id, row[1], row[2])
        now = int(time() * 1000)
        changed = conn.execute(
            'UPDATE runs SET workStartedAt = ?, verifyStartedAt = ?'
            ' WHERE id = ? AND workStartedAt IS NULL AND endedAt IS NULL'
            ' AND EXISTS (SELECT 1 FROM tickets WHERE tickets.id = runs.ticketId'
            ' AND tickets.activeRunId = runs.id)',
            (now, now if verify else None, run_id)).rowcount
        if not changed:
            raise store.ClaimConflict(f'run {run_id} does not own an idle work clock')


@contextmanager
def working(conn, run_id, *, verify=False):
    """Finally-safe role/verify boundary; nested wrappers share the outer span.

    `verify=True` marks the span as verify time. A nested call keeps the
    outer span's kind.

    Only this context's outer owner settles. Other callers cannot take over
    an already running interval; starting work also requires the ticket lease.
    No transaction is held across the work itself.
    """
    key = (conn, run_id)
    if conn is None or run_id is None or key in _active.get():
        yield
        return
    _start_work(conn, run_id, verify)
    token = _active.set(_active.get() | {key})
    try:
        yield
    finally:
        try:
            settle_work(conn, run_id)
        finally:
            _active.reset(token)
