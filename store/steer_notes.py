"""A maintainer's steer on a ticket: a contract amendment, or one turn's hint."""
import time
from typing import NamedTuple

from . import operator_notes
from .operate import record_intervention, record_project_intervention
from .schema import _transaction

AMENDMENT, HINT = "amendment", "hint"
SEALED = ("merged", "abandoned")
PARKED_PHASE = "awaiting_merge_approval"
GATE_PHASES = ("merge_gate", "merging")


class SteerRefused(ValueError):
    pass


class Steer(NamedTuple):
    id: int
    kind: str
    intervention_id: int
    run_id: int | None
    event_id: int | None


class SteerNote(NamedTuple):
    id: int
    kind: str
    note: str
    author: str


class SteerRow(NamedTuple):
    id: int
    ticket: str
    kind: str
    author: str
    note: str
    at: int
    run_id: int | None
    event_id: int | None
    consumed_by: int | None
    consumed_at: int | None


def steer(conn, ticket_id, note, author, hint=False, now=None):
    for name, text in (("note", note), ("author", author)):
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{name} must be non-blank text")
    note, author = note.strip(), author.strip()
    kind = HINT if hint else AMENDMENT
    now = int(time.time() * 1000) if now is None else now
    with _transaction(conn):
        row = conn.execute(
            "SELECT t.linearIdentifier, t.status, t.activeRunId, t.lastRunId,"
            " t.projectId, r.phase, r.prUrl, r.resumePhase, r.outcome"
            " FROM tickets t LEFT JOIN runs r ON r.id = t.lastRunId"
            " WHERE t.id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise SteerRefused(f"ticket {ticket_id} does not exist")
        key, status, live, last, project_id, phase, pr_url, resume, outcome = row
        if live is not None:
            raise SteerRefused(
                f"{key} has live run {live}, which a steer does not reach;"
                f" stop it first with holo pause {key} or holo abort {key}")
        if status in SEALED:
            raise SteerRefused(f"{key} is {status}; nothing is left to steer")
        if status == "blocked_on_operator":
            if phase != PARKED_PHASE or not pr_url:
                raise SteerRefused(
                    f"{key} is blocked_on_operator, parked with no pull request"
                    f" (run {last} is {phase}); a steer reaches a ticket with"
                    " no run in flight or a run parked on its pull request")
            event_id = operator_notes.send_back(conn, last, note, author,
                                                hint=hint)
            (intervention_id,) = conn.execute(
                "SELECT MAX(id) FROM interventions WHERE runId = ?"
                " AND action = 'operator_note'", (last,)).fetchone()
            return _insert(conn, ticket_id, last, kind, note, author, now,
                           intervention_id, event_id)
        if resume in GATE_PHASES or (outcome == "paused"
                                     and resume not in (None, "working")):
            raise SteerRefused(
                f"{key}'s next run resumes run {last}'s candidate at {resume},"
                " past the implement turn, so no implementer or reviewer"
                " would read a steer; steer it once that run parks or ends")
        text = f"{kind} for {key}: {note}"
        if last is None:
            intervention_id = record_project_intervention(
                conn, "steer", text, project_id=project_id, now=now)
        else:
            intervention_id = record_intervention(conn, last, "steer", text,
                                                  now=now)
        return _insert(conn, ticket_id, last, kind, note, author, now,
                       intervention_id, None)


def _insert(conn, ticket_id, run_id, kind, note, author, now, intervention_id,
            event_id):
    note_id = conn.execute(
        "INSERT INTO steerNotes (ticketId, runId, kind, note, author, at,"
        " interventionId, eventId) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, run_id, kind, note, author, now, intervention_id,
         event_id)).lastrowid
    return Steer(note_id, kind, intervention_id, run_id, event_id)


def _notes(conn, where, args):
    return [SteerNote(*row) for row in conn.execute(
        "SELECT id, kind, note, author FROM steerNotes WHERE ticketId = ?"
        f" AND eventId IS NULL AND {where} ORDER BY id", args)]


def amendments(conn, ticket_id):
    return _notes(conn, "kind = ?", (ticket_id, AMENDMENT))


def pending(conn, ticket_id, kind):
    return _notes(conn, "kind = ? AND consumedBy IS NULL", (ticket_id, kind))


def consume(conn, note_ids, run_id, now=None):
    now = int(time.time() * 1000) if now is None else now
    with _transaction(conn):
        for note_id in note_ids:
            conn.execute("UPDATE steerNotes SET consumedBy = ?, consumedAt = ?"
                         " WHERE id = ? AND consumedBy IS NULL",
                         (run_id, now, note_id))


def steers(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                    " AND name = 'steerNotes'").fetchone() is None:
        return []
    return [SteerRow(*row) for row in conn.execute(
        "SELECT s.id, t.linearIdentifier, s.kind, s.author, s.note, s.at,"
        " s.runId, s.eventId, COALESCE(s.consumedBy, c.runId),"
        " COALESCE(s.consumedAt, c.at) FROM steerNotes s"
        " JOIN tickets t ON t.id = s.ticketId"
        " LEFT JOIN runEvents c ON c.id = (SELECT MIN(e.id) FROM runEvents e"
        " WHERE e.kind = 'operator_note_consumed'"
        " AND json_extract(e.payload, '$.event_id') = s.eventId)"
        " ORDER BY s.id")]
