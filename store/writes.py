import time

from .schema import _transaction


def set_board_state(conn, ticket_id, state, column=None):
    with _transaction(conn):
        conn.execute("UPDATE tickets SET boardState = ?,"
                     " boardColumn = COALESCE(?, boardColumn) WHERE id = ?",
                     (state, column, ticket_id))


def set_gone_since(conn, ticket_id, at):
    """Stamp when the board was first seen without the ticket."""
    with _transaction(conn):
        conn.execute("UPDATE tickets SET goneSince = ? WHERE id = ?",
                     (at, ticket_id))


def record_push(conn, ticket_id, state, now=None):
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
    with _transaction(conn):
        conn.execute("UPDATE tickets SET pushState = NULL, pushFrom = NULL,"
                     " pushAt = NULL WHERE id = ?", (ticket_id,))


def set_question(conn, ticket_id, question, *, park_kind=None):
    with _transaction(conn):
        conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                     (question, ticket_id))
        if park_kind is not None:
            conn.execute("UPDATE runs SET parkKind = ? WHERE id ="
                         " (SELECT COALESCE(activeRunId, lastRunId)"
                         " FROM tickets WHERE id = ?)",
                         (park_kind, ticket_id))


def clear_merge_sha(conn, run_id):
    with _transaction(conn):
        conn.execute("UPDATE runs SET mergeSha = NULL WHERE id = ?", (run_id,))


def set_pull_request(conn, run_id, url, candidate_sha=None):
    with _transaction(conn):
        if candidate_sha is None:
            conn.execute("UPDATE runs SET prUrl = ? WHERE id = ?", (url, run_id))
        else:
            conn.execute("UPDATE runs SET prUrl = ?, candidateSha = ? WHERE id = ?",
                         (url, candidate_sha, run_id))


def set_outcome_reason(conn, run_id, reason):
    """Set the reason on a live run; `release()` sets it as it ends one."""
    with _transaction(conn):
        conn.execute("UPDATE runs SET outcomeReason = ? WHERE id = ?",
                     (reason, run_id))


def stamp_board_ask(conn, project_id, at):
    with _transaction(conn):
        conn.execute("UPDATE projects SET boardAskedAt = ? WHERE id = ?",
                     (at, project_id))
