"""Ticket notes: what the factory says on a ticket's board, kept in the store
until the host sweep posts it (KO-742; delivery is KO-747)."""

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


def mark_note_posted(conn, note_id, now=None):
    """Stamp note `note_id` posted at `now` and clear its `postError`: the
    board accepted the comment (KO-747)."""
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        conn.execute("UPDATE ticketNotes SET postedAt = ?, postError = NULL"
                     " WHERE id = ?", (now, note_id))


def mark_note_failed(conn, note_id, error):
    """Record why note `note_id`'s post failed; it stays pending, and the
    next pass posts it again (KO-747)."""
    with _transaction(conn):
        conn.execute("UPDATE ticketNotes SET postError = ? WHERE id = ?",
                     (error, note_id))
