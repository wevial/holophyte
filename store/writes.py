"""Small state writes shared by the loop's board and pull request paths."""

from .schema import _transaction


def set_board_state(conn, ticket_id, state):
    """Set the ticket's mirrored board state."""
    with _transaction(conn):
        conn.execute("UPDATE tickets SET boardState = ? WHERE id = ?",
                     (state, ticket_id))


def set_question(conn, ticket_id, question):
    """Set the operator question, or clear it with None."""
    with _transaction(conn):
        conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                     (question, ticket_id))


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
