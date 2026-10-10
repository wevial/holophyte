"""A cap failure on a steered ticket parks it with a split suggestion."""
import store
from holophyte.board.projection import block_ticket, comment_body
from store import steer_notes

ROUND_CAP_REASON = "terminal adjudication: FAIL"


def cap_hit(failure_kind, reason):
    if failure_kind == "budget":
        return "time cap"
    if (reason or "").startswith(ROUND_CAP_REASON):
        return "review-round cap"
    return None


def question(key, cap, amendments):
    firsts = "; ".join((note.splitlines() or [""])[0] for note in amendments)
    return (f"{key} hit its {cap} after steering added {len(amendments)}"
            f" amendment(s): {firsts}. Split the steered scope into a follow-up"
            f" ticket (holo file), withdraw it here (holo steer {key} --withdraw"
            f" -n NOTE), then holo requeue {key}; or holo requeue {key} to try"
            " again with the amendments.")


def park_steered_cap(conn, run_id, ticket_id, provider):
    kind, reason = conn.execute(
        "SELECT failureKind, outcomeReason FROM runs WHERE id = ?",
        (run_id,)).fetchone()
    cap = cap_hit(kind, reason)
    if cap is None:
        return False
    amendments = steer_notes.standing(conn, ticket_id)
    ticket = store.read.ticket_by_id(conn, ticket_id)
    if not amendments or ticket.status != "in_flight":
        return False
    asked = question(ticket.linearIdentifier, cap, amendments)
    with store.transaction(conn):
        if not block_ticket(conn, ticket_id, provider, asked):
            return False
        store.record_event(conn, run_id, "steer_cap_park", asked)
        if getattr(provider, "store_mode", False) is True:
            store.record_note(conn, ticket_id, "escalation",
                              comment_body(asked), f"steer_cap:{run_id}",
                              run_id=run_id)
    return True
