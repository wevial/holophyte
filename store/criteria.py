import json
import time

from ticket_criteria import split_id, witnesses


def _append(conn, ticket_id, criterion, position, text, kind, target,
            status, source, now):
    conn.execute(
        "INSERT INTO ticketCriteria (ticketId, criterion, position, text,"
        " witnessKind, witnessTarget, status, source, at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, criterion, position, text, kind, target, status, source, now))


def _number(explicit, text, history, taken, highest):
    if explicit is not None and explicit not in taken:
        return explicit
    return next((row[0] for row in history
                 if row[2] == text and row[0] not in taken), highest + 1)


def record_criteria(conn, ticket_id, criteria, commands, now):
    if not criteria:
        return
    history = conn.execute(
        "SELECT criterion, position, text, witnessKind, witnessTarget, status"
        " FROM ticketCriteria WHERE ticketId = ? ORDER BY id", (ticket_id,)
    ).fetchall()
    current = {row[0]: row for row in history}
    highest = max(current, default=0)
    taken = set()
    texts = [split_id(text) for text in criteria]
    for position, ((explicit, text), (kind, target)) in enumerate(
            zip(texts, witnesses([text for _, text in texts], commands)), 1):
        criterion = _number(explicit, text, history, taken, highest)
        taken.add(criterion)
        highest = max(highest, criterion)
        previous = current.get(criterion)
        if previous is None or (previous[1], previous[2], previous[5]) != (
                position, text, "open"):
            _append(conn, ticket_id, criterion, position, text, kind, target,
                    "open", "filed", now)
    for criterion, row in current.items():
        if criterion not in taken and row[5] == "open":
            _append(conn, ticket_id, criterion, None, row[2], row[3], row[4],
                    "retired", "filed", now)


def backfill(conn):
    now = int(time.time() * 1000)
    tickets = conn.execute(
        "SELECT id, acceptanceCriteria, verificationCommands FROM tickets"
        " WHERE status NOT IN ('merged', 'abandoned')"
        " AND NOT EXISTS (SELECT 1 FROM ticketCriteria WHERE ticketId = tickets.id)"
    ).fetchall()
    for ticket_id, criteria, commands in tickets:
        texts = [split_id(text)[1] for text in json.loads(criteria)]
        for position, (text, (kind, target)) in enumerate(
                zip(texts, witnesses(texts, json.loads(commands))), 1):
            _append(conn, ticket_id, position, position, text, kind, target,
                    "open", "backfill", now)
