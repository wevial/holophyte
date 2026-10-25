import store.criteria
import store.read
import store.steer_notes
from holophyte.board.provider import parse_body
from holophyte.tickets.ticket_criteria import split_id


def parse(token):
    return int(str(token).removeprefix("C").removeprefix("c"))


def numbered(conn, run_id, criteria):
    criteria = list(criteria or ())
    snapshot = store.read.run_snapshot(conn, run_id) if conn is not None else None
    revised = adopted(conn, run_id) if snapshot is not None else None
    result = _number_filed(conn, snapshot,
                           criteria if revised is None else revised)
    if snapshot is None:
        return result
    edits = {row["criterion"]: row for row in
             store.criteria.current(conn, snapshot.ticketId)
             if row["steerNoteId"] is not None}
    amended = [(n, edits[n]["text"] if n in edits else text)
               for n, text in result
               if n not in edits or edits[n]["status"] != "retired"]
    present = {n for n, _ in result}
    amended.extend((n, row["text"]) for n, row in edits.items()
                   if n not in present and row["status"] != "retired")
    return amended


def adopted(conn, run_id):
    body = adopted_body(conn, run_id)
    return None if body is None else parse_body("", body)["criteria"]


def adopted_body(conn, run_id):
    row = adopted_ticket_revision(conn, run_id)
    return None if row is None else row.body


def adopted_ticket_revision(conn, run_id):
    revision = store.steer_notes.adopted_revision(conn, run_id)
    return None if revision is None else ticket_revision(conn, run_id, revision)


def claimed_body(conn, run_id):
    (revision,) = conn.execute("SELECT revision FROM runs WHERE id = ?",
                               (run_id,)).fetchone()
    return revision_body(conn, run_id, revision)


def revision_body(conn, run_id, revision):
    row = ticket_revision(conn, run_id, revision)
    return None if row is None else row.body


def ticket_revision(conn, run_id, revision):
    ticket_id = store.read.run_snapshot(conn, run_id).ticketId
    return next((row for row in store.read.ticket_revisions(conn, ticket_id)
                 if row.revision == revision), None)


def _number_filed(conn, snapshot, criteria):
    if not criteria or isinstance(criteria[0], (tuple, list)):
        return criteria
    held = store.criteria.numbers(conn, snapshot.ticketId) if snapshot else {}
    return with_numbers(criteria, held)


def with_numbers(criteria, held):
    texts = [split_id(text) for text in criteria]
    if not held:
        return [(n, text) for n, (_, text) in enumerate(texts, 1)]
    assigned = [explicit if explicit is not None else held.get(text)
                for explicit, text in texts]
    taken = set(held.values()) | {n for n in assigned if n is not None}
    highest = max(taken, default=0)
    result = []
    for position, ((_, text), n) in enumerate(zip(texts, assigned), 1):
        if n is None:
            n = position if position not in taken else highest + 1
        taken.add(n)
        highest = max(highest, n)
        result.append((n, text))
    return result
