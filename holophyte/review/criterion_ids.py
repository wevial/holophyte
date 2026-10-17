import store.criteria
import store.read
from ticket_criteria import split_id


def parse(token):
    return int(str(token).removeprefix("C").removeprefix("c"))


def numbered(conn, run_id, criteria):
    criteria = list(criteria or ())
    if criteria and isinstance(criteria[0], (tuple, list)):
        return criteria
    texts = [split_id(text) for text in criteria]
    snapshot = store.read.run_snapshot(conn, run_id) if conn is not None else None
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
