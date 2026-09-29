"""A story's guards: no child runs before approval, no board Done closes it."""
from holophyte.redact import safe_print as print
from store.notes import record_note
from store.stories import abandon_story, story, witness_ledger
from store.tickets import walk_ticket

CLOSED_STATES = ("closed", "abandoned")


def refusal(conn, ticket_id):
    """Why the claim must not take `ticket_id` now; None lets it go."""
    found = story(conn, ticket_id)
    if found is None or found.state == "approved":
        return None
    return f"its story {_identifier(conn, found.ticketId)} is {found.state}"


def open_story(conn, ticket_id):
    found = story(conn, ticket_id)
    if found is None or found.ticketId != ticket_id \
            or found.state in CLOSED_STATES:
        return None
    return found


def not_green(conn, found):
    rows = witness_ledger(conn, found.ticketId)
    sha = rows[-1].mainSha if rows else None
    green = {row.witnessKey for row in witness_ledger(conn, found.ticketId, sha)
             if row.verdict == "green"} if sha else set()
    return sha, [w.key for w in found.witnesses if w.key not in green]


def held_open(conn, ticket_id, identifier, state):
    found = open_story(conn, ticket_id)
    if found is None or state != "completed":
        return False
    sha, keys = not_green(conn, found)
    at = f"at {sha}" if sha else "with no ledger yet"
    text = (f"Linear holds {identifier} completed, but story {identifier}"
            f" closes on its witnesses at main's tip; not yet green {at}:"
            f" {', '.join(keys) or 'none'}")
    record_note(conn, ticket_id, "reconcile", text,
                f"story:{ticket_id}:completed")
    print(f"[holo2] reconcile left story parent {identifier} open: {text}")
    return True


def walk_closed(conn, ticket_id, identifier, to_status):
    if to_status != "abandoned" or open_story(conn, ticket_id) is None:
        walk_ticket(conn, ticket_id, to_status)
        return
    abandon_story(conn, ticket_id, f"Story {identifier} was abandoned: Linear"
                  " holds its parent canceled.", "factory")


def _identifier(conn, ticket_id):
    return conn.execute("SELECT linearIdentifier FROM tickets WHERE id = ?",
                        (ticket_id,)).fetchone()[0]
