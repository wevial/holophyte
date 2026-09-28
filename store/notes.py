"""Ticket notes, kept in the store until the host sweep posts them."""
import time

from .schema import _transaction


def record_note(conn, ticket_id, kind, text, dedup_key, author="factory",
                run_id=None, now=None):
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
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        conn.execute("UPDATE ticketNotes SET postedAt = ?, postError = NULL"
                     " WHERE id = ?", (now, note_id))


def mark_note_failed(conn, note_id, error):
    with _transaction(conn):
        conn.execute("UPDATE ticketNotes SET postError = ? WHERE id = ?",
                     (error, note_id))
