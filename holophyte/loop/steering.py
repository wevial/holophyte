"""Steer notes recorded on a live run, carried by its next implementer turn."""
from holophyte.babysit.maintainer_notes import steer_text
from store import steer_notes
from store.read import run_snapshot


def pending(conn, run_id):
    if conn is None or run_id is None:
        return []
    return steer_notes.pending(conn, run_snapshot(conn, run_id).ticketId)


def close(conn, run_id):
    return conn is None or run_id is None or steer_notes.close(conn, run_id)


def take(conn, run_id):
    notes = pending(conn, run_id)
    if not notes:
        return ""
    steer_notes.consume(conn, [n.id for n in notes], run_id)
    return "".join(f"{steer_text(n)}\n\n" for n in notes)


def reviewed_ticket(conn, run_id, ticket):
    if conn is None or run_id is None:
        return ticket
    carried = [n for n in steer_notes.amendments(
        conn, run_snapshot(conn, run_id).ticketId)
        if n.live and n.consumed_by == run_id]
    return ticket + "".join(f"\n\n{steer_text(n)}" for n in carried)
