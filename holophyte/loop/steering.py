"""Steer notes recorded on a live run, carried by its next implementer turn."""
import re

from holophyte.babysit.maintainer_notes import render_amendments, steer_text
from holophyte.review.criterion_ids import adopted, adopted_body, claimed_body
from provider import parse_body
from store import operator_notes, steer_notes
from store.read import run_snapshot
from ticket_criteria import amendment_edits


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
    if any(amendment_edits(n.note)[0] for n in notes):
        return render_amendments(conn, run_id, "", notes).lstrip() + "\n\n"
    return "".join(f"{steer_text(n)}\n\n" for n in notes)


def reviewed_ticket(conn, run_id, ticket):
    if conn is None or run_id is None:
        return ticket
    amendments = steer_notes.amendments(
        conn, run_snapshot(conn, run_id).ticketId)
    revised = adopted(conn, run_id) is not None
    if revised or any(amendment_edits(n.note)[0] for n in amendments):
        ticket = re.sub(r"\n\nAmended verify command\(s\):\n.*?(?=\n\n|\Z)",
                        "", ticket, flags=re.DOTALL)
        return render_amendments(conn, run_id, ticket, amendments,
                                 revised) + _verify_block(conn, run_id)
    carried = [n for n in amendments if n.live and n.consumed_by == run_id]
    return ticket + "".join(f"\n\n{steer_text(n)}" for n in carried)


def adopted_verify(conn, run_id, verify_cmd):
    if conn is None or run_id is None:
        return verify_cmd
    return _revised_verify(conn, run_id) or verify_cmd


def _revised_verify(conn, run_id):
    body = adopted_body(conn, run_id)
    adopted_cmd = parse_body("", body)["verify"] if body is not None else None
    claimed = parse_body("", claimed_body(conn, run_id) or "")["verify"]
    return adopted_cmd if adopted_cmd != claimed else None


def _verify_block(conn, run_id):
    adopted_cmd = _revised_verify(conn, run_id)
    if adopted_cmd is None:
        return ""
    return f"\n\nAmended verify command(s):\n{adopted_cmd}"


def take_now(conn, run_id):
    if conn is None or run_id is None:
        return ""
    babysat = steer_notes.babysat_interrupts(conn, run_id)
    if babysat:
        rnd = conn.execute("SELECT MAX(round) FROM reviewRounds WHERE runId = ?",
                           (run_id,)).fetchone()[0]
        operator_notes.consume(conn, run_id, [e for _, e in babysat], rnd)
        steer_notes.consume(conn, [n.id for n, _ in babysat], run_id)
    return take(conn, run_id) + "".join(f"{steer_text(n)}\n\n"
                                        for n, _ in babysat)
