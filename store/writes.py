"""Small state writes shared by the loop's board and pull request paths."""

import time

from .schema import _transaction


def set_board_state(conn, ticket_id, state, column=None):
    """Set the ticket's mirrored board state, and its column when given."""
    with _transaction(conn):
        conn.execute("UPDATE tickets SET boardState = ?,"
                     " boardColumn = COALESCE(?, boardColumn) WHERE id = ?",
                     (state, column, ticket_id))


def set_gone_since(conn, ticket_id, at):
    """Stamp when the board was first seen without the ticket; None clears."""
    with _transaction(conn):
        conn.execute("UPDATE tickets SET goneSince = ? WHERE id = ?",
                     (at, ticket_id))


def record_push(conn, ticket_id, state, now=None):
    """Queue a store-mode push of `state` on the ticket, from the board
    state last observed; answer whether it was queued. A row that already
    shows `state` queues nothing."""
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute("SELECT boardState FROM tickets WHERE id = ?",
                           (ticket_id,)).fetchone()
        if row is None or row[0] == state:
            return False
        conn.execute("UPDATE tickets SET pushState = ?, pushFrom = ?,"
                     " pushAt = ? WHERE id = ?",
                     (state, row[0], now, ticket_id))
        return True


def clear_push(conn, ticket_id):
    """Clear the ticket's queued push: landed, dropped or superseded."""
    with _transaction(conn):
        conn.execute("UPDATE tickets SET pushState = NULL, pushFrom = NULL,"
                     " pushAt = NULL WHERE id = ?", (ticket_id,))


def set_question(conn, ticket_id, question, *, park_kind=None):
    """Set or clear the question, optionally typing the active or last run's park.

    Prose-only updates preserve the kind already recorded by the park writer.
    """
    with _transaction(conn):
        conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                     (question, ticket_id))
        if park_kind is not None:
            conn.execute("UPDATE runs SET parkKind = ? WHERE id ="
                         " (SELECT COALESCE(activeRunId, lastRunId)"
                         " FROM tickets WHERE id = ?)",
                         (park_kind, ticket_id))


def clear_merge_sha(conn, run_id):
    """Clear the factory merge SHA when closing out without a factory merge."""
    with _transaction(conn):
        conn.execute("UPDATE runs SET mergeSha = NULL WHERE id = ?", (run_id,))


def set_pull_request(conn, run_id, url, candidate_sha=None):
    """Set the PR URL, preserving the candidate SHA when none is supplied."""
    with _transaction(conn):
        if candidate_sha is None:
            conn.execute("UPDATE runs SET prUrl = ? WHERE id = ?", (url, run_id))
        else:
            conn.execute("UPDATE runs SET prUrl = ?, candidateSha = ? WHERE id = ?",
                         (url, candidate_sha, run_id))


def set_outcome_reason(conn, run_id, reason):
    """Set the reason even on a live run, without ending it.

    release() is the usual writer; the PR wake breaker updates a live run.
    """
    with _transaction(conn):
        conn.execute("UPDATE runs SET outcomeReason = ? WHERE id = ?",
                     (reason, run_id))


def stamp_board_ask(conn, project_id, at):
    """Record when the supervisor last asked the project's board."""
    with _transaction(conn):
        conn.execute("UPDATE projects SET boardAskedAt = ? WHERE id = ?",
                     (at, project_id))
