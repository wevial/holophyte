"""A maintainer's steer on a ticket: a contract amendment, or one turn's hint."""
import time
from typing import NamedTuple

import store

from . import operator_notes
from .operate import record_intervention, record_project_intervention
from .schema import _transaction

AMENDMENT, HINT = "amendment", "hint"
SEALED = ("merged", "abandoned")
PARKED_PHASE = "awaiting_merge_approval"
GATE_PHASES = ("merge_gate", "merging")
LIVE_PHASES = ("claimed", "working", "verifying", "reviewing", "addressing")


class SteerRefused(ValueError):
    pass


class Steer(NamedTuple):
    id: int
    kind: str
    intervention_id: int
    run_id: int | None
    event_id: int | None
    live: bool = False


class SteerNote(NamedTuple):
    id: int
    kind: str
    note: str
    author: str
    run_id: int | None = None
    consumed_by: int | None = None
    live: bool = False


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
    withdrawn_by: int | None


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
            return _live(conn, ticket_id, key, live, kind, note, author, now,
                         resume)
        if status in SEALED:
            raise SteerRefused(f"{key} is {status}; nothing is left to steer")
        if status == "blocked_on_operator":
            if phase != PARKED_PHASE or not pr_url:
                raise SteerRefused(
                    f"{key} is blocked_on_operator, parked with no pull request"
                    f" (run {last} is {phase}); a steer reaches a ticket with"
                    " no run in flight or a run parked on its pull request")
            return _noted(conn, ticket_id, last, kind, note, author, now,
                          operator_notes.send_back)
        if resume == "merge_gate" and pr_url and outcome != "paused":
            return _noted(conn, ticket_id, last, kind, note, author, now,
                          operator_notes.add_note)
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


def withdraw(conn, ticket_id, note, now=None):
    if not isinstance(note, str) or not note.strip():
        raise ValueError("note must be non-blank text")
    now = int(time.time() * 1000) if now is None else now
    with _transaction(conn):
        row = conn.execute(
            "SELECT linearIdentifier, status, activeRunId, lastRunId, projectId"
            " FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise SteerRefused(f"ticket {ticket_id} does not exist")
        key, status, live, last, project_id = row
        if status in SEALED:
            raise SteerRefused(f"{key} is {status}; nothing is left to steer")
        if live is not None:
            raise SteerRefused(
                f"{key} has live run {live}; withdraw its amendments once that"
                " run parks or ends")
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM steerNotes WHERE ticketId = ? AND kind = ?"
            " AND withdrawnBy IS NULL", (ticket_id, AMENDMENT)).fetchone()
        if not count:
            raise SteerRefused(f"{key} has no amendment to withdraw")
        text = f"withdraw {count} amendment(s) for {key}: {note.strip()}"
        if last is None:
            intervention_id = record_project_intervention(
                conn, "steer", text, project_id=project_id, now=now)
        else:
            intervention_id = record_intervention(conn, last, "steer", text,
                                                  now=now)
        conn.execute("UPDATE steerNotes SET withdrawnBy = ? WHERE ticketId = ?"
                     " AND kind = ? AND withdrawnBy IS NULL",
                     (intervention_id, ticket_id, AMENDMENT))
        return count, intervention_id


def standing(conn, ticket_id):
    return [note for (note,) in conn.execute(
        "SELECT note FROM steerNotes WHERE ticketId = ? AND kind = ?"
        " AND withdrawnBy IS NULL ORDER BY id", (ticket_id, AMENDMENT))]


def withdrawn_events(conn, ticket_id):
    return {event for (event,) in conn.execute(
        "SELECT eventId FROM steerNotes WHERE ticketId = ?"
        " AND eventId IS NOT NULL AND withdrawnBy IS NOT NULL", (ticket_id,))}


def _live(conn, ticket_id, key, live, kind, note, author, now, resume):
    phase, pr_url = conn.execute("SELECT phase, prUrl FROM runs WHERE id = ?",
                                 (live,)).fetchone()
    closed = conn.execute("SELECT 1 FROM runEvents WHERE runId = ?"
                          " AND kind = 'steer_closed'", (live,)).fetchone()
    if phase not in LIVE_PHASES or pr_url or closed:
        raise SteerRefused(
            f"{key}'s live run {live} is in {phase}"
            + (" on its pull request" if pr_url else "")
            + ", past its last implementer turn, so no implementer would read"
            f" a steer; steer it once that run parks or ends, or stop it with"
            f" holo pause {key} or holo abort {key}")
    if phase == "claimed" and resume in GATE_PHASES:
        raise SteerRefused(
            f"{key}'s live run {live} resumes a candidate at {resume}, past"
            " the implement turn, so no implementer would read a steer;"
            " steer it once that run parks or ends")
    intervention_id = record_intervention(conn, live, "steer",
                                          f"{kind} for {key}: {note}", now=now)
    steered = _insert(conn, ticket_id, live, kind, note, author, now,
                      intervention_id, None)
    return steered._replace(live=True)


def _noted(conn, ticket_id, run_id, kind, note, author, now, write):
    event_id = write(conn, run_id, note, author, hint=kind == HINT)
    (intervention_id,) = conn.execute(
        "SELECT MAX(id) FROM interventions WHERE runId = ?"
        " AND action = 'operator_note'", (run_id,)).fetchone()
    return _insert(conn, ticket_id, run_id, kind, note, author, now,
                   intervention_id, event_id)


def _insert(conn, ticket_id, run_id, kind, note, author, now, intervention_id,
            event_id):
    note_id = conn.execute(
        "INSERT INTO steerNotes (ticketId, runId, kind, note, author, at,"
        " interventionId, eventId) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, run_id, kind, note, author, now, intervention_id,
         event_id)).lastrowid
    return Steer(note_id, kind, intervention_id, run_id, event_id)


def _notes(conn, where, args):
    return [SteerNote(*row[:6], bool(row[6])) for row in conn.execute(
        "SELECT s.id, s.kind, s.note, s.author, s.runId, s.consumedBy,"
        " r.id IS NOT NULL AND (r.endedAt IS NULL OR r.endedAt >= s.at)"
        " FROM steerNotes s LEFT JOIN runs r ON r.id = s.runId"
        " WHERE s.ticketId = ? AND s.eventId IS NULL AND s.withdrawnBy IS NULL"
        f" AND {where}"
        " ORDER BY s.id", args)]


def amendments(conn, ticket_id):
    return _notes(conn, "s.kind = ?", (ticket_id, AMENDMENT))


def pending(conn, ticket_id, kind=None):
    if kind is None:
        return _notes(conn, "s.consumedBy IS NULL", (ticket_id,))
    return _notes(conn, "s.kind = ? AND s.consumedBy IS NULL",
                  (ticket_id, kind))


def close(conn, run_id):
    with _transaction(conn):
        waiting = conn.execute(
            "SELECT 1 FROM steerNotes s JOIN runs r ON r.ticketId = s.ticketId"
            " WHERE r.id = ? AND s.eventId IS NULL AND s.consumedBy IS NULL"
            " AND s.withdrawnBy IS NULL",
            (run_id,)).fetchone()
        if waiting:
            return False
        store.record_event(conn, run_id, "steer_closed",
                           "steering closed: no implementer turn remains",
                           level="detail")
        return True


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
        " COALESCE(s.consumedAt, c.at), s.withdrawnBy FROM steerNotes s"
        " JOIN tickets t ON t.id = s.ticketId"
        " LEFT JOIN runEvents c ON c.id = (SELECT MIN(e.id) FROM runEvents e"
        " WHERE e.kind = 'operator_note_consumed'"
        " AND json_extract(e.payload, '$.event_id') = s.eventId)"
        " ORDER BY s.id")]
