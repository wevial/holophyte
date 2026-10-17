import json
import time
from types import SimpleNamespace

from ticket_criteria import split_id, witness_problems, witnesses


def _append(conn, ticket_id, criterion, position, text, kind, target,
            status, source, now, steer_note_id=None):
    conn.execute(
        "INSERT INTO ticketCriteria (ticketId, criterion, position, text,"
        " witnessKind, witnessTarget, status, source, at, steerNoteId)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, criterion, position, text, kind, target, status, source, now,
         steer_note_id))


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
    latest = {row["criterion"]: row for row in current(conn, ticket_id)}
    highest = max((row[0] for row in history), default=0)
    taken = set()
    texts = [split_id(text) for text in criteria]
    for position, ((explicit, text), (kind, target)) in enumerate(
            zip(texts, witnesses([text for _, text in texts], commands)), 1):
        criterion = _number(explicit, text, history, taken, highest)
        taken.add(criterion)
        highest = max(highest, criterion)
        previous = latest.get(criterion)
        if previous is None or previous["steerNoteId"] is not None or (
                previous["position"], previous["text"], previous["status"]) != (
                position, text, "open"):
            _append(conn, ticket_id, criterion, position, text, kind, target,
                    "open", "filed", now)
    for criterion, row in latest.items():
        if criterion not in taken and row["status"] == "open":
            _append(conn, ticket_id, criterion, None, row["text"],
                    row["witnessKind"], row["witnessTarget"],
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


def numbers(conn, ticket_id):
    rows = conn.execute(
        "SELECT c.text, c.criterion FROM ticketCriteria c"
        " LEFT JOIN steerNotes s ON s.id = c.steerNoteId"
        " WHERE c.ticketId = ? AND s.withdrawnBy IS NULL"
        " ORDER BY c.steerNoteId IS NOT NULL, c.id DESC", (ticket_id,)).fetchall()
    result = {}
    for text, criterion in rows:
        result.setdefault(text, criterion)
    return result


def current(conn, ticket_id):
    columns = ("id", "criterion", "position", "text", "witnessKind",
               "witnessTarget", "status", "source", "steerNoteId")
    rows = conn.execute(
        "SELECT " + ", ".join("c." + column for column in columns)
        + " FROM ticketCriteria c LEFT JOIN steerNotes s ON s.id = c.steerNoteId"
        " WHERE c.ticketId = ? AND s.withdrawnBy IS NULL ORDER BY c.id DESC",
        (ticket_id,)).fetchall()
    latest = {}
    for row in rows:
        latest.setdefault(row[1], dict(zip(columns, row)))
    return sorted(latest.values(), key=lambda row: (
        row["status"] == "retired", row["position"] is None,
        row["position"] or 0, row["criterion"]))


def amend(conn, ticket_id, edits, steer_note_id, now):
    from .steer_notes import SteerRefused

    latest = {row["criterion"]: row for row in current(conn, ticket_id)}
    highest = conn.execute(
        "SELECT COALESCE(MAX(criterion), 0) FROM ticketCriteria WHERE ticketId = ?",
        (ticket_id,)).fetchone()[0]
    commands = json.loads(conn.execute(
        "SELECT verificationCommands FROM tickets WHERE id = ?",
        (ticket_id,)).fetchone()[0])
    for verb, number, text in edits:
        if verb != "ADD" and (number not in latest
                                  or latest[number]["status"] == "retired"):
            raise SteerRefused(f"{verb} C{number} names no current criterion")
        if verb == "DROP":
            row = latest[number]
            _append(conn, ticket_id, number, None, row["text"],
                    row["witnessKind"], row["witnessTarget"], "retired",
                    "amendment", now, steer_note_id)
            latest[number] = {**row, "status": "retired"}
            continue
        problems = witness_problems(SimpleNamespace(
            acceptance=[text], acceptance_done=[], acceptance_other=[],
            verify_commands=commands))
        if problems:
            raise SteerRefused("; ".join(problems))
        if verb == "ADD":
            highest += 1
            number = highest
            position = max((row["position"] or 0 for row in latest.values()),
                           default=0) + 1
        else:
            position = latest[number]["position"]
        kind, target = witnesses([text], commands)[0]
        _append(conn, ticket_id, number, position, text, kind, target, "open",
                "amendment", now, steer_note_id)
        latest[number] = dict(position=position, text=text,
                              witnessKind=kind, witnessTarget=target, status="open")
