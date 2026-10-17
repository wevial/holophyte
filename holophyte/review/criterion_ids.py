import store.criteria
import store.read
from ticket_criteria import split_id


def parse(token):
    return int(str(token).removeprefix("C").removeprefix("c"))


def numbered(conn, run_id, criteria):
    criteria = list(criteria or ())
    snapshot = store.read.run_snapshot(conn, run_id) if conn is not None else None
    result = _number_filed(conn, snapshot, criteria)
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


def _number_filed(conn, snapshot, criteria):
    if not criteria or isinstance(criteria[0], (tuple, list)):
        return criteria
    texts = [split_id(text) for text in criteria]
    held = store.criteria.numbers(conn, snapshot.ticketId) if snapshot else {}
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
