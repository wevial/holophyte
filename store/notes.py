"""Ticket notes: what the factory says on a ticket's board, kept in the store
until the host sweep posts it (KO-742; delivery is DRAFT-465)."""

import time

from .schema import _transaction


def record_note(conn, ticket_id, kind, text, dedup_key, author="factory",
                run_id=None, now=None):
    """Write one note of `kind` on `ticket_id`; return its id, or None when
    the ticket already has a note under `dedup_key`, so the same entry
    written twice is one note. Joins a caller's `transaction()` when one is
    open -- a ledger entry and its note land together -- and otherwise is
    one of its own. `text` is stored as given: the caller cleans and caps it
    as the board is to see it."""
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        cursor = conn.execute(
            "INSERT INTO ticketNotes (ticketId, runId, at, author, kind,"
            " dedupKey, text) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (ticketId, dedupKey) DO NOTHING",
            (ticket_id, run_id, now, author, kind, dedup_key, text))
    return cursor.lastrowid if cursor.rowcount else None
